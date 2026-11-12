#!/usr/bin/env python3
"""Trusted workflow publisher. The editor receives no publication credential.

Embedded into workflow YAML, never imported or executed from the PR checkout.
The Git object transfer uses a fresh bare repository and never runs PR hooks.
"""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import stat
import tempfile
import urllib.parse
import urllib.request

SHA = re.compile(r"[0-9a-f]{40}")
INSTRUCTIONS = re.compile(r"(^|/)(\.claude(?:/|$)|CLAUDE\.md$|CLAUDE\.local\.md$|AGENTS\.md$)|^(\.mcp\.json|\.claude\.json|\.gitmodules|\.ripgreprc)$|^\.husky(?:/|$)|^\.github(?:$|/(workflows|actions)(?:/|$))")


def git_env():
    keep = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
            "SSL_CERT_FILE", "SSL_CERT_DIR", "GIT_SSL_CAINFO"}
    return {**{k: v for k, v in os.environ.items() if k in keep},
            "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false"}


def git(args, cwd, *, env=None, timeout=30):
    return subprocess.run(["/usr/bin/git", "-c", "core.fsmonitor=false", "-c", "core.commitGraph=false", *args], cwd=cwd, env=env or git_env(),
                          capture_output=True, timeout=timeout, check=True).stdout


def api(path, *, method="GET", body=None):
    # Read/receipt operations always use the caller's built-in token. It is
    # never replaced with the publication credential after a denied read.
    request = urllib.request.Request(os.environ["GITHUB_API_URL"].rstrip('/')+'/'+path,
        headers={"Authorization": "Bearer "+os.environ["READ_TOKEN"],
                 "Accept": "application/vnd.github+json", "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None, method=method)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def publish(result=None):
    workspace = Path(os.environ["GITHUB_WORKSPACE"])
    expected, branch = os.environ["EXPECTED_HEAD"], os.environ["HEAD_REF"]
    repo = os.environ["GITHUB_REPOSITORY"]
    if result is None:
        result = {"outcome": "unverified", "published_sha": ""}
    if not SHA.fullmatch(expected) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        return result
    git(["check-ref-format", "refs/heads/"+branch], workspace)
    if branch in {"main", "master"}:
        return result
    candidate = git(["rev-parse", "--verify", "HEAD^{commit}"], workspace).decode().strip()
    if not SHA.fullmatch(candidate):
        return result
    result["candidate_sha"] = candidate
    if candidate == expected:
        result["outcome"] = "no_change"
        return result
    try:
        git(["merge-base", "--is-ancestor", expected, candidate], workspace)
    except subprocess.CalledProcessError:
        result['outcome'] = 'candidate_ancestry_held'
        return result
    subjects = git(['log','--format=%s','--no-show-signature',expected+'..'+candidate], workspace).decode().splitlines()
    subjects_valid = bool(subjects) and all(subject.startswith('[autofix]') for subject in subjects)
    # Inspect every commit, including both sides of merges. A net-zero change
    # to an instruction file must not hide it in published history.
    names = git(["log", "-m", "--format=", "--name-only", "--no-ext-diff", "--no-textconv",
                 "--no-renames", "-z", expected+".."+candidate], workspace).decode().split('\0')
    if any(INSTRUCTIONS.search(name) for name in names):
        result["outcome"] = "instruction_surface_held"
        return result
    # Inspect committed objects only. Reading the editor's working tree could
    # invoke its clean filters; uncommitted work is never publication evidence.
    server = os.environ['GITHUB_SERVER_URL'].rstrip('/')
    if urllib.parse.urlparse(server).scheme != 'https':
        return result
    with tempfile.TemporaryDirectory(prefix='trusted-autofix-publisher-') as directory:
        root = Path(directory)
        (root/'empty-template').mkdir()
        # Local object transfer cannot invoke upload-pack or PR hooks. The
        # fresh config never inherits the editor's URL rewrites or credentials.
        git(['clone', '--bare', '--local', '--shared', '--no-hardlinks',
             '--template='+str(root/'empty-template'), str(workspace), str(root/'objects.git')], root, timeout=120)
        # Recheck actual commit parents in the controller's fresh repository.
        # Editor-owned grafts, replace refs and commit-graph caches cannot grant
        # a non-fast-forward update through the expected-ref lease.
        git(['--git-dir='+str(root/'objects.git'),'merge-base','--is-ancestor',expected,candidate], root)
        recovery = Path(tempfile.mkdtemp(prefix='autofix-recovery-', dir=os.environ['RUNNER_TEMP']))
        git(['--git-dir='+str(root/'objects.git'), 'update-ref', 'refs/heads/implementation', candidate], root)
        git(['--git-dir='+str(root/'objects.git'), 'bundle', 'create', str(recovery/'implementation.bundle'),
             'refs/heads/implementation', '^'+expected], root, timeout=120)
        (recovery/'receipt.json').write_text(json.dumps(dict(repository=repo,
            pr_number=os.environ['PR_NUMBER'], base_sha=expected, candidate_sha=candidate,
            scope='Git objects only; never execute recovery payloads. Revalidate native custody/head and owning gates before publication.'))+'\n')
        result['recovery_path'] = str(recovery)
        if not subjects_valid:
            result['outcome'] = 'commit_subject_held'
            return result
        # Local data retention is independent of publication authority. A late
        # owner/head/read hold must not discard implementation on runner exit.
        if os.environ.get('PUBLICATION_ADMITTED') != 'true':
            result['outcome'] = 'publication_admission_held'
            return result
        result['outcome'] = 'native_state_unverified'
        pr = api(f"repos/{repo}/pulls/{os.environ['PR_NUMBER']}")
        if (pr['state'] != 'open' or pr['head']['sha'] != expected or pr['head']['ref'] != branch
                or pr['head']['repo']['full_name'].casefold() != repo.casefold()
                or branch in {pr['base']['ref'], pr['base']['repo'].get('default_branch')}
                or any(x['name'] in {'skip-ai-review','human-merge','security-remediation'} for x in pr['labels'])
                or re.search(r'^X-Follow-Through-Owner:', re.sub(r'```.*?```', '', pr.get('body') or '', flags=re.S), re.M)):
            result['outcome'] = 'native_state_held'
            return result
        token = os.environ.get("PUBLISH_TOKEN", "")
        if not token:
            result['outcome'] = 'publication_credential_unavailable'
            return result
        env = git_env()
        env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0=f'http.{server}/.extraheader',
            GIT_CONFIG_VALUE_0='AUTHORIZATION: basic '+base64.b64encode(('x-access-token:'+token).encode()).decode())
        result['outcome'] = 'publication_unknown'  # Before the sole send.
        try:
            # Ancestry was verified above with replacement objects disabled.
            # This lease adds an atomic expected-ref check; it cannot authorize
            # a history rewrite or recreate a branch deleted after admission.
            git(['--git-dir='+str(root/'objects.git'), 'push',
                 '--force-with-lease=refs/heads/'+branch+':'+expected, server+'/'+repo+'.git',
                 candidate+':refs/heads/'+branch], root, env=env, timeout=120)
        except subprocess.CalledProcessError:
            pass  # Read back once; never blindly resend an uncertain push.
        ref = api(f"repos/{repo}/git/ref/heads/{urllib.parse.quote(branch, safe='')}")
        if ref['object']['sha'] == candidate:
            result.update(outcome='published', published_sha=candidate)
    return result


def record(result):
    outcome = result.get('outcome')
    if outcome in {'no_change', 'unverified'} or not outcome:
        return
    repo, number, head = (os.environ[k] for k in ('GITHUB_REPOSITORY','PR_NUMBER','EXPECTED_HEAD'))
    if not SHA.fullmatch(head) or not re.fullmatch(r'[1-9][0-9]*', number):
        return
    marker = f'<!-- pack-autofix-publication:v1 head={head} outcome={outcome} -->'
    for page in range(1,6):
        comments = api(f'repos/{repo}/issues/{number}/comments?per_page=100&page={page}')
        if any(c.get('user',{}).get('id') == 41898282 and marker in (c.get('body') or '') for c in comments):
            return
        if len(comments) < 100:
            break
    else:
        return  # Incomplete history is not permission to duplicate a receipt.
    dependencies = {
        'publication_credential_unavailable': 'The owning CI-triggering publication credential is unavailable. Restore its approved scoped route before publication.',
        'publication_admission_held': 'Native custody admission declined. The retained owner must resolve the reported custody/read/hold dependency before publication.',
        'native_state_held': 'The native PR head/state/repository/hold changed. Reconcile the current native state before recovering or publishing the candidate.',
        'instruction_surface_held': 'The candidate changed protected instruction or workflow-control paths. Route this finding to the original owner and its approved trusted-source publication route; finish other authorized findings before recovering or publishing this candidate.',
        'commit_subject_held': 'The candidate violated the existing [autofix] accounting convention. The original owner must review and recommit the retained repair with valid subjects.',
        'candidate_ancestry_held': 'The candidate is not verified as a descendant of the admitted head. The original owner must reconcile workspace/head ownership and restore the authorized branch.',
        'native_state_unverified': 'Native PR readback is unverified. Restore the same approved read route and revalidate current custody/head; do not switch identities.',
        'publication_packet_held': 'The editor data packet could not be bound to native PR/head inputs or exceeded its size budget. Inspect this run artifact without executing it; reconcile or reduce the original repair before another publication attempt.',
        'publication_unknown': 'The sole push has an uncertain outcome. Verify the authenticated remote SHA before considering another send.',
    }
    body = ('Verified autofix publication: `'+result['published_sha']+'`. New-head CI still needs verification.'
            if outcome == 'published' else
            'Publication held: `'+outcome+'`. '+dependencies.get(outcome,'The controller result needs owning review.')+
            ' The parent task remains unfinished. Other authorized commitments can proceed. '+
            ('The original owner must verify and recover retained implementation before advancing this head. '
             'A recovery path alone is not a durable upload receipt.' if result.get('recovery_path') else
             'This controller retained no recovery bundle; reconcile the original editor run before advancing this head.'))
    run_id = os.environ.get('GITHUB_RUN_ID','')
    if re.fullmatch(r'[1-9][0-9]*',run_id):
        body += ' Inspect the original run and verify its artifact upload: '+os.environ['GITHUB_SERVER_URL'].rstrip('/')+'/'+repo+'/actions/runs/'+run_id
    api(f'repos/{repo}/issues/{number}/comments', method='POST', body={'body':marker+'\n'+body})


def prepare_packet():
    """Unprivileged editor-side data capture; never reads a publisher secret/API."""
    work = Path(os.environ['GITHUB_WORKSPACE'])
    expected = os.environ['EXPECTED_HEAD']
    if not SHA.fullmatch(expected):
        raise ValueError('invalid admitted head')
    candidate = git(['rev-parse','--verify','HEAD^{commit}'], work).decode().strip()
    if not SHA.fullmatch(candidate):
        raise ValueError('invalid candidate')
    # Foreign history is not retained as an owned repair.
    git(['merge-base','--is-ancestor',expected,candidate],work)
    packet = Path(tempfile.mkdtemp(prefix='autofix-packet-',dir=os.environ['RUNNER_TEMP']))
    if candidate != expected:
        with tempfile.TemporaryDirectory(prefix='autofix-packet-objects-') as directory:
            root = Path(directory); (root/'template').mkdir()
            git(['clone','--bare','--local','--shared','--no-hardlinks','--template='+str(root/'template'),
                 str(work),str(root/'objects.git')],root,timeout=120)
            git(['--git-dir='+str(root/'objects.git'),'update-ref','refs/heads/implementation',candidate],root)
            git(['--git-dir='+str(root/'objects.git'),'bundle','create',str(packet/'implementation.bundle'),
                 'refs/heads/implementation','^'+expected],root,timeout=120)
    (packet/'receipt.json').write_text(json.dumps(dict(repository=os.environ['GITHUB_REPOSITORY'],
        pr_number=os.environ['PR_NUMBER'],head_ref=os.environ['HEAD_REF'],base_sha=expected,candidate_sha=candidate))+'\n')
    return dict(outcome='prepared',published_sha='',recovery_path=str(packet))


def import_packet(directory):
    """Fresh publisher runner: parse data only, bind it to trusted native inputs."""
    packet = Path(os.environ['AUTOFIX_PACKET_DIR'])
    receipt = packet/'receipt.json'
    if not stat.S_ISREG(receipt.lstat().st_mode) or not 0 < receipt.stat().st_size <= 16384:
        raise ValueError('invalid packet receipt')
    meta = json.loads(receipt.read_text())
    for field, env in [('repository','GITHUB_REPOSITORY'),('pr_number','PR_NUMBER'),
                       ('head_ref','HEAD_REF'),('base_sha','EXPECTED_HEAD')]:
        if meta.get(field) != os.environ[env]:
            raise ValueError('packet does not match native admission')
    expected, candidate = meta['base_sha'], meta.get('candidate_sha','')
    if not SHA.fullmatch(expected) or not SHA.fullmatch(candidate):
        raise ValueError('invalid packet object')
    expected_files = {'receipt.json'} if candidate == expected else {'receipt.json','implementation.bundle'}
    if {p.name for p in packet.iterdir()} != expected_files:
        raise ValueError('unexpected packet data')
    if candidate == expected:
        return None
    bundle = packet/'implementation.bundle'
    if not stat.S_ISREG(bundle.lstat().st_mode) or not 0 < bundle.stat().st_size <= 64*1024*1024:
        raise ValueError('invalid or over-budget bundle')
    root = Path(directory); (root/'template').mkdir()
    work = root/'work'; work.mkdir()
    git(['init','--quiet','--bare','--template='+str(root/'template')],work)
    server = os.environ['GITHUB_SERVER_URL'].rstrip('/')
    repo = os.environ['GITHUB_REPOSITORY']
    if (urllib.parse.urlparse(server).scheme != 'https'
            or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo)):
        raise ValueError('invalid native repository')
    env = git_env()
    env.update(GIT_CONFIG_COUNT='1',GIT_CONFIG_KEY_0=f'http.{server}/.extraheader',
        GIT_CONFIG_VALUE_0='AUTHORIZATION: basic '+base64.b64encode(('x-access-token:'+os.environ['READ_TOKEN']).encode()).decode())
    # The only network acquisition is the native admitted base, using the read
    # token. No checkout, filters, PR config or editor environment is imported.
    git(['fetch','--no-tags','--no-recurse-submodules','--depth=1',server+'/'+repo+'.git',expected],work,env=env,timeout=120)
    git(['bundle','verify',str(bundle)],work)
    refs = git(['bundle','list-heads',str(bundle)],work).decode().splitlines()
    if refs != [candidate+' refs/heads/implementation']:
        raise ValueError('unexpected bundle references')
    git(['bundle','unbundle',str(bundle)],work,timeout=120)
    git(['update-ref','refs/heads/implementation',candidate],work)
    git(['symbolic-ref','HEAD','refs/heads/implementation'],work)
    return work


def main():
    result = {'outcome':'unverified','published_sha':''}
    try:
        if os.environ.get('AUTOFIX_MODE') == 'prepare':
            result = prepare_packet()
        elif os.environ.get('AUTOFIX_PACKET_DIR'):
            result['outcome'] = 'publication_packet_held'
            with tempfile.TemporaryDirectory(prefix='autofix-import-') as directory:
                work = import_packet(directory)
                if work is None:
                    result['outcome'] = 'no_change'
                else:
                    os.environ['GITHUB_WORKSPACE'] = str(work)
                    result = publish(result)
        else:
            result = publish(result)
    except Exception as exc:
        result['failure_class'] = type(exc).__name__  # No payloads, tokens or Git stderr.
    with open(os.environ['GITHUB_OUTPUT'],'a') as stream:
        for key in ('outcome','published_sha','recovery_path'):
            stream.write(key+'='+result.get(key,'')+'\n')
    print(json.dumps(result))
    try:
        if os.environ.get('AUTOFIX_MODE') != 'prepare':
            record(result)
    except Exception:
        print('Publication receipt delivery unverified; no duplicate send attempted.')
    return 0 if result['outcome'] in {'published','no_change','prepared'} else 2


if __name__ == '__main__':
    raise SystemExit(main())
