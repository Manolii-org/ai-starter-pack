#!/usr/bin/env python3
"""Embed the canonical custody/publisher code in trusted workflow source.

The called workflow never imports controller code from a consumer's PR checkout.
"""
import argparse
from pathlib import Path
import re
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]


def step(kind, identity, condition, number, head, branch=None, token=None):
    name = 'Check retained execution owner' if kind == 'guard' else 'Publish and verify the repair'
    env = {'REPOSITORY': '${{ github.repository }}', 'PR_NUMBER': number, 'EXPECTED_HEAD': head}
    if kind == 'guard':
        env['GH_TOKEN'] = '${{ github.token }}'
        body = (ROOT/'scripts/ci/autofix-custody.sh').read_text().rstrip()
    else:
        env.update(HEAD_REF=branch, READ_TOKEN='${{ github.token }}', PUBLISH_TOKEN=token,
                   PUBLICATION_ADMITTED="${{ steps.custody_publish.outputs.skip == 'false' }}")
        body = "/usr/bin/python3 -I - <<'AUTOFIX_PY'\n"+(ROOT/'scripts/ci/autofix-publish.py').read_text().rstrip()+"\nAUTOFIX_PY"
    lines = [f'      - name: {name}',f'        id: {identity}',f'        if: {condition}', '        env:']
    lines += [f'          {key}: {value}' for key,value in env.items()]
    lines += ['        run: |',f'          # autofix-control:{kind}:begin']
    lines += ['          '+line if line else '' for line in body.splitlines()]
    lines += [f'          # autofix-control:{kind}:end','']
    return '\n'.join(lines)+'\n'


def initial(text, reusable):
    number = '${{ steps.ref.outputs.pr_number }}' if reusable else '${{ needs.preflight.outputs.pr_number }}'
    head = '${{ steps.ref.outputs.head_sha }}' if reusable else '${{ needs.preflight.outputs.head_sha }}'
    branch = '${{ steps.ref.outputs.head_ref }}' if reusable else '${{ needs.preflight.outputs.head_ref }}'
    condition = ("steps.ref.outputs.skip != 'true' && steps.pr.outputs.skip != 'true' && steps.budget.outputs.skip != 'true'"
                 if reusable else "steps.lock.outputs.go == 'true'")
    text = text.replace('  contents: write','  contents: read')
    # Acquisition and the model only receive the built-in read-only code token.
    text = re.sub(r'(token: )\$\{\{ secrets\.(?:GH_PAT|MN_GH_AUTOMATION_TOKEN) \|\| (?:secrets.GITHUB_TOKEN|github.token) \}\}',
                  r'\1${{ github.token }}', text)
    text = text.replace('and push to the PR branch','locally; do not push. The trusted controller owns publication')
    if reusable:
        text = text.replace("if: steps.ref.outputs.skip != 'true'", "if: steps.ref.outputs.skip != 'true' && steps.custody.outputs.skip != 'true'")
        guard = step('guard','custody',"steps.ref.outputs.skip != 'true'",number,head)
        start = text.index('      - uses: actions/checkout@')
        text = text[:start]+guard+text[start:]
        # A retained session does not need the legacy editor's credentials.
        a=text.index('      - name: Validate provider secrets');b=text.index('      - name: Resolve PR number',a)
        provider=text[a:b].replace('        env:',"        if: steps.ref.outputs.skip != 'true' && steps.custody.outputs.skip != 'true'\n        env:",1)
        text=text[:a]+text[b:]
        start=text.index('      - uses: actions/checkout@');text=text[:start]+provider+text[start:]
        text=text.replace('      activation: ${{ steps.verify_exec',
            '      publication: ${{ steps.publish.outputs.outcome }}\n      published_sha: ${{ steps.publish.outputs.published_sha }}\n      activation: ${{ steps.verify_exec')
        text=text.replace('    outputs:\n      activation:',
            '    outputs:\n      published_sha:\n        description: Verified remote publication SHA; empty does not establish handling.\n        value: ${{ jobs.autofix.outputs.published_sha }}\n      publication:\n        description: Controller outcome, separate from editor execution.\n        value: ${{ jobs.autofix.outputs.publication }}\n      activation:',1)
    else:
        guard=step('guard','custody',"steps.ref.outputs.skip != 'true' && steps.pr.outputs.skip != 'true'",
                   '${{ steps.ref.outputs.pr_number }}','${{ steps.pr.outputs.head_sha }}')
        start=text.index('      - name: Enforce per-PR successful-fix budget');text=text[:start]+guard+text[start:]
        text=text.replace("if: steps.ref.outputs.skip != 'true' && steps.pr.outputs.skip != 'true'\n        env:\n          GH_TOKEN:",
                          "if: steps.ref.outputs.skip != 'true' && steps.pr.outputs.skip != 'true' && steps.custody.outputs.skip != 'true'\n        env:\n          GH_TOKEN:")
        text=text.replace('          BUDGET_SKIP:', '          CUSTODY_SKIP: ${{ steps.custody.outputs.skip }}\n          BUDGET_SKIP:')
        text=text.replace('if [[ "$REF_SKIP" == "true" ||', 'if [[ "$CUSTODY_SKIP" == "true" || "$REF_SKIP" == "true" ||')
        text=text.replace("if: steps.lock.outputs.go == 'true'", "if: steps.lock.outputs.go == 'true' && steps.custody_dispatch.outputs.skip != 'true'")
        start=text.index('      - uses: actions/checkout@');text=text[:start]+step('guard','custody_dispatch',condition,number,head)+text[start:]
        text=text.replace('    needs: preflight\n', '    needs: preflight\n    outputs:\n      publication: ${{ steps.publish.outputs.outcome }}\n      published_sha: ${{ steps.publish.outputs.published_sha }}\n')
    # Re-read native custody immediately before both editor and publisher.
    marker='      - name: Run autofix with Claude Code'
    start=text.index(marker)
    if reusable:
        text=text[:start]+step('guard','custody_dispatch',condition,number,head)+text[start:]
        end=text.index('        env:',text.index(marker))
        part=text[text.index(marker):end].replace('        if: '+condition,
            '        if: '+condition+" && steps.custody_dispatch.outputs.skip != 'true'")
        text=text[:text.index(marker)]+part+text[end:]
    start=text.index('      - name: Verify autofix commits left the instruction surface alone')
    guard=step('guard','custody_publish',"steps.verify_exec.outcome == 'success'",number,head)
    pub=step('publish','publish',"steps.verify_exec.outcome == 'success' && steps.custody_publish.outputs.skip == 'false'",
             number,head,branch,'${{ secrets.GH_PAT }}' if reusable else '${{ secrets.MN_GH_AUTOMATION_TOKEN }}')
    text=text[:start]+guard+pub+text[start:]
    recovery = '''      - name: Retain unpublished implementation
        if: always() && steps.publish.outputs.recovery_path != '' && steps.publish.outputs.outcome != 'published'
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7
        with:
          name: autofix-recovery-${{ github.run_id }}-${{ github.run_attempt }}
          path: ${{ steps.publish.outputs.recovery_path }}
          retention-days: 7
          if-no-files-found: error

'''
    start=text.index('      - name: Verify autofix commits left the instruction surface alone')
    text=text[:start]+recovery+text[start:]
    return text


def render(text):
    # Retention runs even when the final guard holds publication. The protected
    # controller consumes the guard decision after capturing safe Git objects.
    text = text.replace("id: publish\n        if: steps.verify_exec.outcome == 'success' && steps.custody_publish.outputs.skip == 'false'",
                        "id: publish\n        if: steps.verify_exec.outcome == 'success'")
    text = re.sub(r"(?m)^(          PUBLISH_TOKEN: .*)$",
        r"\1\n          PUBLICATION_ADMITTED: ${{ steps.custody_publish.outputs.skip == 'false' }}",text)
    text = text.replace("          PUBLICATION_ADMITTED: ${{ steps.custody_publish.outputs.skip == 'false' }}\n"*2,
                        "          PUBLICATION_ADMITTED: ${{ steps.custody_publish.outputs.skip == 'false' }}\n")
    text = text.replace("steps.budget.outputs.skip == 'true') && 'skipped'",
        "steps.budget.outputs.skip == 'true' || steps.custody.outputs.skip == 'true' || steps.custody_dispatch.outputs.skip == 'true') && 'skipped'")
    # The publisher owns the one material credential-hold receipt. The old
    # preflight warning both repeats it and advertises a removed token fallback.
    text = re.sub(r'(?ms)^      - name: Warn when elevated push token is absent\n.*?(?=^      - name: Resolve Anthropic credential\n)', '', text)
    text=text.replace('6. If architectural / ambiguous, post `gh pr comment` and STOP — do not guess',
        '6. A genuine external or human dependency holds only its affected finding. Record its dependency, unblock condition and next action once; finish other already-authorized findings before yielding. Never retry unchanged denials, repeat wait notices or mark the parent task done from a partial result.')
    if '      - name: Retain unpublished implementation' not in text:
        recovery = """      - name: Retain unpublished implementation
        if: always() && steps.publish.outputs.recovery_path != '' && steps.publish.outputs.outcome != 'published'
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7
        with:
          name: autofix-recovery-${{ github.run_id }}-${{ github.run_attempt }}
          path: ${{ steps.publish.outputs.recovery_path }}
          retention-days: 7
          if-no-files-found: error

"""
        start=text.index('      - name: Verify autofix commits left the instruction surface alone')
        text=text[:start]+recovery+text[start:]
    for kind, source in [('guard','scripts/ci/autofix-custody.sh'),('publish','scripts/ci/autofix-publish.py')]:
        body=(ROOT/source).read_text().rstrip()
        if kind=='publish':body="/usr/bin/python3 -I - <<'AUTOFIX_PY'\n"+body+"\nAUTOFIX_PY"
        body='\n'.join('          '+line if line else '' for line in body.splitlines())
        pattern=rf'(?ms)(^          # autofix-control:{kind}:begin\n).*?(^          # autofix-control:{kind}:end$)'
        text=re.sub(pattern,lambda m:m[1]+body+'\n'+m[2],text)
    budget=(ROOT/'scripts/ci/autofix-budget-notice.sh').read_text().rstrip()
    budget='\n'.join('            '+line if line else '' for line in budget.splitlines())
    text=re.sub(r'(?ms)(^            # autofix-control:budget-notice:begin\n).*?(^            # autofix-control:budget-notice:end$)',
                lambda m:m[1]+budget+'\n'+m[2],text)
    text=text.replace('      - name: Verify the agent executed\n        if:',
                      '      - name: Verify the agent executed\n        id: verify_exec\n        if:')
    start=text.index('      - name: Run autofix with Claude Code')
    end=text.index('        env:',start)
    prefix=text[start:end]
    if 'steps.custody_dispatch.outputs.skip' not in prefix:
        prefix=re.sub(r'(?m)^(        if: .*)$',r"\1 && steps.custody_dispatch.outputs.skip != 'true'",prefix)
    text=text[:start]+prefix+text[end:]
    text=re.sub(r"(id: custody_dispatch\n        if: steps.ref[^\n]*)(\n)",
        lambda m:m[1]+(" && steps.custody.outputs.skip != 'true'" if 'steps.custody.outputs' not in m[1] else '')+m[2],text)
    for guard in ('custody','custody_dispatch'):
        text = text.replace(f"steps.{guard}.outputs.skip != 'true'", f"steps.{guard}.outputs.skip == 'false'")
    return text


def validate(text, reusable):
    """Refuse partial/misordered control wiring; unset outputs never admit work."""
    jobs = yaml.safe_load(text)['jobs']
    expected = {'autofix': ('custody_dispatch','claude','verify_exec','custody_publish','publish')}
    if reusable:
        expected['autofix'] = ('custody',*expected['autofix'])
    else:
        expected['preflight'] = ('custody','budget')
        needs = jobs['autofix'].get('needs',[])
        if 'preflight' not in ([needs] if isinstance(needs,str) else needs):
            raise ValueError('autofix must depend on custody preflight')
    canonical = (ROOT/'scripts/ci/autofix-custody.sh').read_text().rstrip()
    for name, required in expected.items():
        steps = jobs[name]['steps'];ids = [s.get('id') for s in steps]
        if any(ids.count(identity)!=1 for identity in required):
            raise ValueError('missing or duplicate required custody/control step')
        if [ids.index(identity) for identity in required] != sorted(ids.index(identity) for identity in required):
            raise ValueError('custody/control steps are out of order')
        for s in steps:
            if s.get('id') in ('custody','custody_dispatch','custody_publish') and canonical not in s.get('run',''):
                raise ValueError('custody guard payload is missing')
    editor = next(s for s in jobs['autofix']['steps'] if s.get('id')=='claude')
    if "steps.custody_dispatch.outputs.skip == 'false'" not in editor.get('if',''):
        raise ValueError('editor requires affirmative custody admission')
    publisher = next(s for s in jobs['autofix']['steps'] if s.get('id')=='publish')
    if ((ROOT/'scripts/ci/autofix-publish.py').read_text().rstrip() not in publisher.get('run','')
            or publisher.get('env',{}).get('PUBLICATION_ADMITTED') != "${{ steps.custody_publish.outputs.skip == 'false' }}"
            or publisher.get('if') != "steps.verify_exec.outcome == 'success'"):
        raise ValueError('publisher payload or admission is missing')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--check',action='store_true');args=parser.parse_args()
    stale=[]
    for name in ['pr-autofix-loop.yml','pr-autofix-loop-reusable.yml']:
        path=ROOT/'.github/workflows'/name;text=path.read_text()
        result=render(initial(text,'reusable' in name) if '# autofix-control:guard:begin' not in text else text)
        try:
            validate(result,'reusable' in name)
        except (ValueError,KeyError,TypeError,yaml.YAMLError):
            print(f'INVALID custody/control structure: {path.name}',file=sys.stderr)
            return 1
        if result!=text:
            stale.append(str(path.relative_to(ROOT)))
            if not args.check:path.write_text(result)
    print(('STALE' if args.check and stale else 'OK')+' autofix controller source')
    return int(args.check and bool(stale))


if __name__=='__main__':raise SystemExit(main())
