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
import tempfile
import urllib.parse
import urllib.request

SHA = re.compile(r"[0-9a-f]{40}")
INSTRUCTIONS = re.compile(r"(^|/)(\.claude/|CLAUDE\.md$|CLAUDE\.local\.md$|AGENTS\.md$)|^(\.mcp\.json|\.claude\.json|\.gitmodules|\.ripgreprc)$|^\.husky/")


def git_env():
    keep = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
            "SSL_CERT_FILE", "SSL_CERT_DIR", "GIT_SSL_CAINFO"}
    return {**{k: v for k, v in os.environ.items() if k in keep},
            "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false"}


def git(args, cwd, *, env=None, timeout=30):
    return subprocess.run(["/usr/bin/git", "-c", "core.fsmonitor=false", *args], cwd=cwd, env=env or git_env(),
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
    git(["merge-base", "--is-ancestor", expected, candidate], workspace)
    names = git(["diff", "--name-only", "--no-ext-diff", "--no-textconv", "--no-renames", "-z",
                 expected, candidate], workspace).decode().split('\0')
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
        recovery = Path(tempfile.mkdtemp(prefix='autofix-recovery-', dir=os.environ['RUNNER_TEMP']))
        git(['--git-dir='+str(root/'objects.git'), 'update-ref', 'refs/heads/implementation', candidate], root)
        git(['--git-dir='+str(root/'objects.git'), 'bundle', 'create', str(recovery/'implementation.bundle'),
             'refs/heads/implementation', '^'+expected], root, timeout=120)
        (recovery/'receipt.json').write_text(json.dumps(dict(repository=repo,
            pr_number=os.environ['PR_NUMBER'], base_sha=expected, candidate_sha=candidate,
            scope='Git objects only; never execute recovery payloads. Revalidate native custody/head and owning gates before publication.'))+'\n')
        result['recovery_path'] = str(recovery)
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
            git(['--git-dir='+str(root/'objects.git'), 'push', server+'/'+repo+'.git',
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
    body = ('Verified autofix publication: `'+result['published_sha']+'`. New-head CI still needs verification.'
            if outcome == 'published' else
            'Autofix execution did not establish publication: `'+outcome+'`. The parent task remains unfinished; '
            'the existing owner must resolve this specific dependency before retrying publication. '
            'Independent authorized implementation is not globally blocked.')
    api(f'repos/{repo}/issues/{number}/comments', method='POST', body={'body':marker+'\n'+body})


def main():
    result = {'outcome':'unverified','published_sha':''}
    try:
        result = publish(result)
    except Exception as exc:
        result['failure_class'] = type(exc).__name__  # No payloads, tokens or Git stderr.
    with open(os.environ['GITHUB_OUTPUT'],'a') as stream:
        for key in ('outcome','published_sha','recovery_path'):
            stream.write(key+'='+result.get(key,'')+'\n')
    print(json.dumps(result))
    try:
        record(result)
    except Exception:
        print('Publication receipt delivery unverified; no duplicate send attempted.')
    return 0 if result['outcome'] in {'published','no_change'} else 2


if __name__ == '__main__':
    raise SystemExit(main())
