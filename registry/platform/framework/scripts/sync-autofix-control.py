#!/usr/bin/env python3
"""Embed protected controllers and validate editor/publisher job isolation.

The public callers retain their API and locks. Only the internal execution
workflow contains code; it never imports controller code from a PR checkout.
"""
import argparse
import ast
from pathlib import Path
import re
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXECUTION = 'pr-autofix-execution-reusable.yml'
EDITOR_CONDITIONS = {
    'standalone': "steps.lock.outputs.go == 'true' && steps.custody_dispatch.outputs.skip == 'false'",
    'reusable': "steps.ref.outputs.skip != 'true' && steps.custody.outputs.skip == 'false' && steps.pr.outputs.skip != 'true' && steps.budget.outputs.skip != 'true' && steps.custody_dispatch.outputs.skip == 'false'",
}
CAPTURE_CONDITION = "always() && steps.claude.outcome != 'skipped' && steps.claude.outcome != ''"
ADMISSIONS = {
    'standalone': "${{ steps.lock.outputs.go == 'true' && steps.custody_publish.outputs.skip == 'false' }}",
    'reusable': "${{ steps.ref.outputs.skip == 'false' && steps.pr.outputs.skip == 'false' && steps.budget.outputs.skip == 'false' && steps.custody_publish.outputs.skip == 'false' }}",
}


class Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def represent_string(dumper, value):
    if '\n' in value:
        value = '\n'.join(line.rstrip() for line in value.split('\n'))
    return dumper.represent_scalar('tag:yaml.org,2002:str', value, style='|' if '\n' in value else None)


Dumper.add_representer(str, represent_string)


def normalized(value):
    return ' '.join(value.split())


def prepare_source():
    """Small explicit source closure; editor code has no publication/API path."""
    source = (ROOT/'scripts/ci/autofix-publish.py').read_text()
    tree = ast.parse(source)
    selected = []
    for node in tree.body:
        if (isinstance(node,(ast.Import,ast.ImportFrom))
                or isinstance(node,ast.FunctionDef) and node.name in {'git_env','git','prepare_packet'}
                or isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id == 'SHA' for t in node.targets)):
            selected.append(ast.get_source_segment(source,node))
    return '\n\n'.join(selected)+"\n\nresult = prepare_packet()\nwith open(os.environ['GITHUB_OUTPUT'],'a') as stream:\n    stream.write('recovery_path='+result['recovery_path']+'\\n')\nprint(json.dumps(result))"


def render(text):
    data = yaml.safe_load(text)
    if True in data:
        data['on'] = data.pop(True)
    for job in data['jobs'].values():
        for step in job.get('steps', []):
            body = step.get('run', '')
            for kind, source in [('guard','scripts/ci/autofix-custody.sh'),
                                 ('prepare','scripts/ci/autofix-publish.py'),
                                 ('publish','scripts/ci/autofix-publish.py')]:
                if f'# autofix-control:{kind}:begin' not in body:
                    continue
                payload = prepare_source() if kind == 'prepare' else (ROOT/source).read_text().rstrip()
                if kind != 'guard':
                    payload = "/usr/bin/python3 -I - <<'AUTOFIX_PY'\n"+payload+"\nAUTOFIX_PY"
                body = re.sub(rf'(?ms)^# autofix-control:{kind}:begin\n.*?^# autofix-control:{kind}:end$',
                    lambda _: f'# autofix-control:{kind}:begin\n{payload}\n# autofix-control:{kind}:end', body)
            if '# autofix-control:budget-notice:begin' in body:
                notice = (ROOT/'scripts/ci/autofix-budget-notice.sh').read_text().rstrip()
                body = re.sub(r'(?ms)^  # autofix-control:budget-notice:begin\n.*?^  # autofix-control:budget-notice:end$',
                    lambda _: '  # autofix-control:budget-notice:begin\n'+'\n'.join('  '+s if s else '' for s in notice.splitlines())+'\n  # autofix-control:budget-notice:end',body)
            if 'run' in step:
                step['run'] = body
    data = {k:data[k] for k in ('name','on',*tuple(k for k in data if k not in ('name','on')))}
    return '# Trusted control source: generated payloads are checked by scripts/sync-autofix-control.py.\n'+yaml.dump(data,Dumper=Dumper,sort_keys=False,width=140).replace("\n'on':","\non:")


def validate(text, reusable=None):
    """Require whole approved gates and separate fresh publisher runners."""
    data = yaml.safe_load(text); jobs = data['jobs']
    canonical = (ROOT/'scripts/ci/autofix-custody.sh').read_text().rstrip()
    publisher_source = (ROOT/'scripts/ci/autofix-publish.py').read_text().rstrip()
    if data['permissions']['contents'] != 'read':
        raise ValueError('editor contents permission must be read-only')
    for variant in ('standalone', 'reusable'):
        editor_job, publisher_job = jobs[variant+'_editor'], jobs[variant+'_publisher']
        steps, pub_steps = editor_job['steps'], publisher_job['steps']
        required = ('custody_dispatch','claude','verify_exec','prepare')
        if variant == 'reusable':
            required = ('custody', *required)
        ids = [s.get('id') for s in steps]
        pub_ids = [s.get('id') for s in pub_steps]
        if any(ids.count(x) != 1 for x in required) or any(pub_ids.count(x) != 1 for x in ('custody_publish','publish')):
            raise ValueError('missing or duplicate required custody/control step')
        if [ids.index(x) for x in required] != sorted(ids.index(x) for x in required) or pub_ids.index('custody_publish') >= pub_ids.index('publish'):
            raise ValueError('custody/control steps are out of order')
        for s in steps+pub_steps:
            if s.get('id') in ('custody','custody_dispatch','custody_publish') and canonical not in s.get('run',''):
                raise ValueError('custody guard payload is missing')
        editor = next(s for s in steps if s.get('id') == 'claude')
        if normalized(editor.get('if','')) != EDITOR_CONDITIONS[variant]:
            raise ValueError('editor requires the full affirmative custody condition')
        prepare = next(s for s in steps if s.get('id') == 'prepare')
        if (prepare.get('env',{}).get('AUTOFIX_MODE') != 'prepare' or prepare_source() not in prepare.get('run','')
                or normalized(prepare.get('if','')) != CAPTURE_CONDITION):
            raise ValueError('unprivileged packet preparation is missing')
        if any('secrets.GH_PAT' in str(s) or 'secrets.MN_GH_AUTOMATION_TOKEN' in str(s) for s in steps) or any('PUBLISH_TOKEN' in k for k in editor_job.get('env',{})):
            raise ValueError('publication credentials must not enter the editor job')
        publisher = next(s for s in pub_steps if s.get('id') == 'publish')
        if (publisher_source not in publisher.get('run','') or publisher.get('if') is not None
                or normalized(publisher.get('env',{}).get('PUBLICATION_ADMITTED','')) != ADMISSIONS[variant]
                or publisher.get('env',{}).get('AUTOFIX_PACKET_DIR') != '${{ runner.temp }}/autofix-candidate-data'):
            raise ValueError('publisher payload or native admission is missing')
        if (normalized(publisher_job.get('if','')) != f"needs.{variant}_editor.result == 'success' && needs.{variant}_editor.outputs.activation == 'active'"
                or publisher_job.get('runs-on') != 'ubuntu-latest' or publisher_job.get('needs') != variant+'_editor'
                or publisher_job.get('timeout-minutes') != 5 or editor_job.get('timeout-minutes') != 25
                or any('checkout@' in s.get('uses','') or 'claude-code-action@' in s.get('uses','') for s in pub_steps)
                or publisher_job.get('env')):
            raise ValueError('publisher must run separately without PR code/environment')
        download = [s for s in pub_steps if s.get('uses','').startswith('actions/download-artifact@')]
        if len(download) != 1 or download[0]['with'] != {
            'name':'autofix-candidate-${{ github.run_id }}-${{ github.run_attempt }}',
            'path':'${{ runner.temp }}/autofix-candidate-data'}:
            raise ValueError('candidate handoff must be bound to this run and attempt')


def validate_wrapper(text, reusable):
    data = yaml.safe_load(text); job = data['jobs']['autofix']
    if job.get('uses') != '$/.github/workflows/'+EXECUTION or job['with'].get('variant') != ('reusable' if reusable else 'standalone'):
        raise ValueError('caller must invoke the complete isolated execution workflow')
    if 'steps' in job or 'runs-on' in job:
        raise ValueError('caller must hold its lock over both execution jobs')
    if reusable:
        if 'concurrency' in job or 'concurrency' in data:
            raise ValueError('reusable caller owns concurrency; do not nest its lock')
    else:
        steps = data['jobs']['preflight']['steps']; ids = [s.get('id') for s in steps]
        if (ids.count('custody') != 1 or ids.count('budget') != 1 or ids.index('custody') > ids.index('budget')
                or (ROOT/'scripts/ci/autofix-custody.sh').read_text().rstrip() not in steps[ids.index('custody')].get('run','')):
            raise ValueError('standalone custody admission must precede budget and locking')
    if not reusable and (job.get('needs') != 'preflight' or job.get('concurrency') != {
            'group':'autofix-${{ github.repository }}-${{ needs.preflight.outputs.pr_number }}',
            'cancel-in-progress':False} or 'concurrency' in data):
        raise ValueError('standalone admission precedes the noncancelling whole-execution lock')


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--check',action='store_true'); args = parser.parse_args()
    stale = []
    for name in ['pr-autofix-loop.yml','pr-autofix-loop-reusable.yml',EXECUTION]:
        path = ROOT/'.github/workflows'/name; text = path.read_text(); result = render(text)
        try:
            if name == EXECUTION:
                validate(result)
            else:
                validate_wrapper(result,'reusable' in name)
        except (ValueError,KeyError,TypeError,yaml.YAMLError) as exc:
            print(f'INVALID custody/control structure: {path.name}: {exc}',file=sys.stderr)
            return 1
        if result != text:
            stale.append(str(path.relative_to(ROOT)))
            if not args.check:
                path.write_text(result)
    print(('STALE' if args.check and stale else 'OK')+' autofix controller source')
    return int(args.check and bool(stale))


if __name__ == '__main__':
    raise SystemExit(main())
