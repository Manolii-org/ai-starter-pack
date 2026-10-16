"""Execute receipt and publisher preparation from the actual workflow definitions."""
import ast
import json
import os
import re
import subprocess
import shutil
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("lane,raw,expected", [
    ("code", "codex\nNo actionable defects.", "completed"),
    ("code", "startup logs only", "failed_output"),
    ("code", "codex\nYou have reached your Codex usage limits for code reviews.", "blocked_quota"),
    ("security", '{"findings":[]}', "completed"),
    ("security", None, "failed_output"),
    ("security", '{"findings":[1]}', "failed_output"),
    ("security", '{"findings":[{"title":"an issue","severity":"high"}]}', "completed"),
])
def test_actual_receipt_and_comment_steps(tmp_path, lane, raw, expected):
    trusted_dir = tmp_path / "codex-trusted"
    trusted_dir.mkdir()
    shutil.copyfile(ROOT / "scripts/ci/codex_review_receipt.py", trusted_dir / "codex_review_receipt.py")
    workflow = "codex-pr-review.yml" if lane == "code" else "codex-security-diff-scan.yml"
    data = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    job = data["jobs"]["review" if lane == "code" else "scan"]
    record = next(s for s in job["steps"] if s.get("id") == "receipt")
    report = next(s for s in job["steps"] if s.get("id") == "report")
    output_dir = tmp_path / "codex-security-diff"
    output_dir.mkdir()
    input_file = tmp_path / "codex-review-out.txt" if lane == "code" else output_dir / "findings.json"
    if raw is not None:
        input_file.write_text(raw)
    env = os.environ | {"RUNNER_TEMP": str(tmp_path), "GITHUB_REPOSITORY": "example/project",
        "PR_NUM": "1", "HEAD_SHA": "a" * 40, "BASE_SHA": "b" * 40,
        "RANGE_STATE": "ready", "RANGE_SCANNABLE": "true", "AUTH_PRESENT": "true",
        "INSTALL_OUTCOME": "success", "SCAN_OUTCOME": "success", "SCAN_RC": "0",
        "SCAN_WITHHELD": "false", "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"), "REVIEW_OUT": str(input_file),
        "OUT_DIR": str(output_dir), "RUN_URL": "https://github.com/example/project/actions/runs/1"}
    command = record["run"].replace("${{ runner.temp }}", str(tmp_path))
    subprocess.run(["bash", "-c", command], cwd=ROOT, env=env, check=True, capture_output=True, text=True)
    result = json.loads((tmp_path / f"codex-{lane}-receipt.json").read_text())
    assert result["state"] == expected
    env["REVIEW_STATE"] = result["state"]
    subprocess.run(["bash", "-c", report["run"]], cwd=ROOT, env=env, check=True,
                   capture_output=True, text=True)
    comment_path = tmp_path / ("codex-review-comment.md" if lane == "code" else "codex-diff-scan-comment.md")
    text = comment_path.read_text()
    marker = re.search(r"<!-- codex-review-receipt:v1 (.+?) -->", text)
    assert marker and json.loads(marker.group(1)) == result
    if expected != "completed":
        assert "No completed" in text
        assert "No findings in diff" not in text


def test_all_workflow_shell_blocks_parse():
    for path in (ROOT / ".github/workflows").glob("codex-*.yml"):
        data = yaml.safe_load(path.read_text())
        for job in data["jobs"].values():
            for step in job.get("steps", []):
                if "run" in step:
                    result = subprocess.run(["bash", "-n"], input=step["run"], text=True,
                                            capture_output=True, timeout=5)
                    assert result.returncode == 0, (path.name, step.get("name"), result.stderr)


@pytest.mark.parametrize('lane,cli,native,actor,stale,passed', [
    ('code', '', 'formal', 'chatgpt-codex-connector[bot]', False, True),
    ('code', '', 'inline', 'chatgpt-codex-connector[bot]', False, True),
    ('code', 'blocked_quota', 'inline', 'chatgpt-codex-connector[bot]', False, True),
    ('code', '', 'inline_reply', 'chatgpt-codex-connector[bot]', False, False),
    ('code', '', 'inline_no_review', 'chatgpt-codex-connector[bot]', False, False),
    ('code', '', 'inline_stale', 'chatgpt-codex-connector[bot]', False, False),
    ('code', '', 'inline', 'github-actions[bot]', False, False),
    ('code', '', 'security_inline', 'chatgpt-codex-connector[bot]', False, False),
    ('security', '', 'inline', 'chatgpt-codex-connector[bot]', False, False),
    ('code', '', 'inline_unmarked', 'chatgpt-codex-connector[bot]', False, False),
    ('code', '', 'quota_summary', 'chatgpt-codex-connector[bot]', False, False),
    ('code', '', 'misleading_summary', 'chatgpt-codex-connector[bot]', False, False),
    ('security', '', 'mixed_summary', 'chatgpt-codex-connector[bot]', False, True),
    ('code', '', 'mixed_summary', 'chatgpt-codex-connector[bot]', False, False),
    ('security', '', 'ambiguous_summary', 'chatgpt-codex-connector[bot]', False, False),
    ('code', '', None, 'chatgpt-codex-connector[bot]', False, False),
    ('security', '', 'security', 'chatgpt-codex-connector[bot]', False, True),
    ('code', 'completed', None, 'chatgpt-codex-connector[bot]', False, True),
    ('code', 'blocked_quota', 'formal', 'chatgpt-codex-connector[bot]', False, True),
    ('code', 'blocked_quota', 'security', 'chatgpt-codex-connector[bot]', False, False),
    ('code', 'blocked_quota', None, 'chatgpt-codex-connector[bot]', False, False),
    ('code', 'blocked_quota', 'formal', 'github-actions[bot]', False, False),
    ('code', 'completed', None, 'chatgpt-codex-connector[bot]', True, False),
    ('security', 'blocked_quota', 'security', 'chatgpt-codex-connector[bot]', False, True),
    ('security', 'blocked_quota', 'formal', 'chatgpt-codex-connector[bot]', False, False),
])
def test_actual_completion_job_native_fallback(lane, cli, native, actor, stale, passed):
    workflow = 'codex-pr-review.yml' if lane == 'code' else 'codex-security-diff-scan.yml'
    data = yaml.safe_load((ROOT / '.github/workflows' / workflow).read_text())
    script = data['jobs']['completion']['steps'][0]['with']['script']
    runner = r'''
const fs=require('fs'),p=JSON.parse(fs.readFileSync(0,'utf8'));
const sha='a'.repeat(40), messages=[], failures=[];
const item={user:{login:p.actor},body:'Codex Review',commit_id:sha,state:'COMMENTED'};
const bodies={security:'| 🔒 **Security Review** | ✅ **Completed** | `aaaaaaaaaa` |',
quota_summary:'| 📝 **Code Review** | ⚠️ **Usage limits reached** | `aaaaaaaaaa` |',
misleading_summary:'| 📝 **Code Review** | ⚠️ **Usage limits reached** | `aaaaaaaaaa` | ✅ **Completed** |',
mixed_summary:'| 📝 **Code Review** | ⚠️ **Usage limits reached** | `aaaaaaaaaa` |\n| 🔒 **Security Review** | ✅ **Completed** | `aaaaaaaaaa` |',
ambiguous_summary:'| 🔒 **Security Review** | ✅ **Completed** | `aaaaaaaaaa` |'};
const comments=bodies[p.native]?[{user:{login:p.actor},body:bodies[p.native]}]:[];
const api={repos:{getCommit:async()=>{if(p.native==='ambiguous_summary')throw Error('422 ambiguous');return {data:{sha}};}},pulls:{get:async()=>({data:{head:{sha:p.stale?'b'.repeat(40):sha}}}),listReviews:async()=>({data:p.native==='formal'?[item]:[]}),listReviewComments:async()=>({data:['inline','inline_stale','security_inline','inline_unmarked','inline_reply','inline_no_review'].includes(p.native)?[{user:{login:p.actor},commit_id:p.native==='inline_stale'?'b'.repeat(40):sha,path:'src/file.ts',pull_request_review_id:p.native==='inline_no_review'?null:123,in_reply_to_id:p.native==='inline_reply'?44:null,body:p.native==='inline_unmarked'?'arbitrary text':'a finding\n<!-- codex-'+(p.native==='security_inline'?'security':'code')+'-review-finding:v1 -->'}]:[]})},issues:{listComments:async()=>({data:comments})}};
const github={rest:api,paginate:async fn=>(await fn()).data};
const core={info:s=>messages.push(s),setFailed:s=>failures.push(s)};
const context={repo:{owner:'example',repo:'project'},payload:{pull_request:{number:1}}};
const proc={env:{EXPECTED_HEAD:sha,REVIEW_HEAD:sha,REVIEW_STATE:p.cli}};
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
new AsyncFunction('github','core','context','process',p.script)(github,core,context,proc).then(()=>process.stdout.write(JSON.stringify({messages,failures}))).catch(e=>{console.error(e);process.exit(1);});
'''
    result = subprocess.run(['node', '-e', runner], input=json.dumps({'script':script,'native':native,'actor':actor,'stale':stale,'cli':cli}), capture_output=True, text=True, check=True, timeout=10)
    assert (not json.loads(result.stdout)['failures']) == passed


@pytest.mark.parametrize('body,expected', [
    ('Codex review: Didn’t find any major issues.\nreviewed commit: aaaaaaaaaa', ['codex-pr-review.yml']),
    ('| 📝 **Code Review** | ✅ **Completed** | `aaaaaaaaaa` |', ['codex-pr-review.yml']),
    ('| 🔒 **Security Review** | ✅ **Completed** | `aaaaaaaaaa` |', ['codex-security-diff-scan.yml']),
    ('| 📝 **Code Review** | ✅ **Completed** | `aaaaaaaaaa` |\n| 🔒 **Security Review** | ✅ **Completed** | `aaaaaaaaaa` |', ['codex-pr-review.yml','codex-security-diff-scan.yml']),
    ('inline', ['codex-pr-review.yml']),
    ('inline_stale', []),
    ('quota_inline', []),
    ('security_inline', []),
    ('You have reached your Codex usage limits for code reviews.', 0),
    ('Codex review: Didn’t find any major issues.\nReviewed commit: bbbbbbbbbb', 0),
])
def test_actual_recovery_reruns_only_completion_jobs(body, expected):
    data = yaml.safe_load((ROOT / '.github/workflows/codex-review-recovery.yml').read_text())
    script = data['jobs']['recover']['steps'][0]['with']['script']
    runner = r'''
const fs=require('fs'),p=JSON.parse(fs.readFileSync(0,'utf8')),sha='a'.repeat(40),rerun=[];
const pull={state:'open',draft:false,head:{sha}};
const github={rest:{pulls:{get:async()=>({data:pull})},actions:{
listWorkflowRuns:async args=>({data:{workflow_runs:[{id:'other-pr',head_sha:sha,path:'.github/workflows/'+args.workflow_id,conclusion:'failure',pull_requests:[{number:99}]},{id:args.workflow_id,head_sha:sha,path:'.github/workflows/'+args.workflow_id,conclusion:'failure',pull_requests:[{number:1}]}]}}),
listJobsForWorkflowRun:async args=>({data:[{id:1,name:'Codex review (subscription)',conclusion:'failure'},{id:args.run_id,name:args.run_id==='codex-pr-review.yml'?'Codex review coverage':'Codex security coverage',conclusion:'failure'}]}),
reRunJobForWorkflowRun:async args=>rerun.push(args.job_id)
}},paginate:async(fn,args)=>(await fn(args)).data};
const context={repo:{owner:'example',repo:'project'},payload:{issue:{number:1},comment:{body:p.body,commit_id:['inline','quota_inline'].includes(p.body)?sha:'b'.repeat(40),path:'src/file.ts',pull_request_review_id:123}}};
if(['inline','inline_stale','security_inline','quota_inline'].includes(p.body)){context.payload.pull_request={number:1};delete context.payload.issue;}
if(['inline','inline_stale','security_inline','quota_inline'].includes(p.body))context.payload.comment.body='a finding\n<!-- codex-'+(p.body==='security_inline'?'security':'code')+'-review-finding:v1 -->';
if(p.body==='quota_inline')context.payload.comment.body='You have reached your Codex usage limits for code reviews.\n<!-- codex-code-review-finding:v1 -->';
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
new AsyncFunction('github','core','context',p.script)(github,{info:()=>{}},context).then(()=>process.stdout.write(JSON.stringify(rerun))).catch(e=>{console.error(e);process.exit(1);});
'''
    result = subprocess.run(['node', '-e', runner], input=json.dumps({'script':script,'body':body}), capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(result.stdout) == (expected if isinstance(expected, list) else [])


@pytest.mark.parametrize('scanner_rc,expected', [(0, 'completed'), (9, 'failed_scan')])
def test_actual_security_scan_exit_is_bound_to_receipt(tmp_path, scanner_rc, expected):
    """Execute the YAML scan step with a fake scanner, not a prefilled rc."""
    data = yaml.safe_load((ROOT / '.github/workflows/codex-security-diff-scan.yml').read_text())
    steps = data['jobs']['scan']['steps']
    scan = next(s['run'] for s in steps if s.get('id') == 'scan')
    record = next(s['run'] for s in steps if s.get('id') == 'receipt')
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    for name in ('git', 'sudo'):
        f = bindir / name
        f.write_text('#!/bin/sh\nexit 0\n')
        f.chmod(0o700)
    f = bindir / 'codex-security'
    f.write_text('#!/bin/sh\nprintf \'{"findings":[]}\\n\' > "$RUNNER_TEMP/codex-security-diff/findings.json"\nexit "$MOCK_SCANNER_RC"\n')
    f.chmod(0o700)
    output = tmp_path / 'outputs'
    env = {**os.environ, 'PATH': str(bindir) + os.pathsep + os.environ['PATH'],
           'RUNNER_TEMP': str(tmp_path), 'GITHUB_OUTPUT': str(output),
           'HEAD_SHA': 'a' * 40, 'BASE_SHA': 'b' * 40, 'MOCK_SCANNER_RC': str(scanner_rc)}
    result = subprocess.run(['bash', '-c', scan], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == scanner_rc
    assert output.read_text().strip() == f'rc={scanner_rc}'
    env.update(GITHUB_REPOSITORY='example/project', PR_NUM='1', RANGE_STATE='ready',
               AUTH_PRESENT='true', INSTALL_OUTCOME='success', SCAN_WITHHELD='false',
               SCAN_OUTCOME='success' if result.returncode == 0 else 'failure',
               SCAN_RC=output.read_text().strip().split('=')[1])
    trusted_dir = tmp_path / "codex-trusted"
    trusted_dir.mkdir()
    shutil.copyfile(ROOT / "scripts/ci/codex_review_receipt.py", trusted_dir / "codex_review_receipt.py")
    record = record.replace('${{ runner.temp }}', str(tmp_path))
    subprocess.run(['bash', '-c', record], cwd=ROOT, env=env, capture_output=True, text=True, check=True, timeout=10)
    assert json.loads((tmp_path / 'codex-security-receipt.json').read_text())['state'] == expected


@pytest.mark.parametrize('outcome,withheld,allowed', [
    ('success', 'false', True), ('success', 'true', False),
    ('failure', '', False), ('cancelled', '', False), ('skipped', '', False),
])
def test_raw_security_artifact_cannot_upload_without_redaction(outcome, withheld, allowed):
    doc = yaml.safe_load((ROOT / '.github/workflows/codex-security-diff-scan.yml').read_text())
    step = next(s for s in doc['jobs']['scan']['steps'] if s.get('name') == 'Upload findings artifact')
    expression = step['if'].replace('always()', 'True').replace('&&', ' and ')
    expression = expression.replace('steps.auth.outputs.present', repr('true'))
    expression = expression.replace('steps.redact.outcome', repr(outcome))
    expression = expression.replace('steps.redact.outputs.withheld', repr(withheld))
    def evaluate(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
            return all(evaluate(value) for value in node.values)
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq):
            return evaluate(node.left) == evaluate(node.comparators[0])
        raise AssertionError('Unexpected artifact-admission expression')
    assert evaluate(ast.parse(expression, mode='eval').body) is allowed


@pytest.mark.parametrize("workflow,job", [("codex-pr-review.yml", "review"), ("codex-security-diff-scan.yml", "scan")])
def test_secret_handling_executes_base_helper_not_pr_helper(tmp_path, workflow, job):
    steps = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())["jobs"][job]["steps"]
    trusted = next(step for step in steps if step.get("id") == "trusted")
    assert steps.index(trusted) < next(i for i, step in enumerate(steps) if step.get("id") == "auth")
    checkout = tmp_path / "repo"
    checkout.mkdir()
    helper = checkout / "scripts/ci/codex_review_receipt.py"
    helper.parent.mkdir(parents=True)
    helper.write_text('print("trusted-base")\n')
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    subprocess.run(["git", "add", "."], cwd=checkout, check=True)
    subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "base"], cwd=checkout, check=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    helper.write_text('raise RuntimeError("PR helper must never execute")\n')
    env = os.environ | {"RUNNER_TEMP": str(tmp_path), "BASE_SHA": base}
    subprocess.run(["bash", "-c", trusted["run"]], cwd=checkout, env=env, check=True, capture_output=True)
    copied = tmp_path / "codex-trusted/codex_review_receipt.py"
    result = subprocess.run(["python3", "-I", str(copied)], cwd=checkout, env=env, check=True, capture_output=True, text=True)
    assert result.stdout.strip() == "trusted-base"
    for step in steps:
        command = step.get("run", "")
        if "python3" in command and "codex_review_receipt.py" in command:
            assert 'python3 -I "$RUNNER_TEMP/codex-trusted/codex_review_receipt.py"' in command
            assert "python3 scripts/" not in command
    # Failed base lookup cannot leave a cached helper usable by always() steps.
    env["BASE_SHA"] = "0" * 40
    result = subprocess.run(["bash", "-c", trusted["run"]], cwd=checkout, env=env, capture_output=True)
    assert result.returncode != 0 and not copied.exists()
    receipt = next(step for step in steps if step.get("id") == "receipt")
    assert "steps.trusted.outcome == 'success'" in receipt["if"]


def test_actual_range_step_keeps_renamed_sensitive_source(tmp_path):
    steps = yaml.safe_load((ROOT / ".github/workflows/codex-security-diff-scan.yml").read_text())["jobs"]["scan"]["steps"]
    checkout = tmp_path / "repo"
    checkout.mkdir()
    config = checkout / "config"
    config.mkdir()
    (config / "human-merge-policy.yaml").write_text("labels: [human-merge]\n")
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=checkout, text=True).strip()
    git("init", "-q")
    git("config", "user.name", "test")
    git("config", "user.email", "test@example.com")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    git("mv", "config/human-merge-policy.yaml", "config/ordinary.yaml")
    git("commit", "-qam", "rename control")
    head = git("rev-parse", "HEAD")
    # Establish the reported failure under the old git invocation.
    assert git("diff", "--name-only", base + "..." + head) == "config/ordinary.yaml"
    trusted = tmp_path / "codex-trusted"
    trusted.mkdir()
    shutil.copyfile(ROOT / "scripts/ci/codex_review_receipt.py", trusted / "codex_review_receipt.py")
    command = next(step["run"] for step in steps if step.get("id") == "range")
    command = command.replace("${{ github.event.pull_request.base.sha }}", base).replace("${{ github.event.pull_request.head.sha }}", head)
    env = os.environ | {"RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(tmp_path / "output"), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary")}
    subprocess.run(["bash", "-c", command], cwd=checkout, env=env, check=True, capture_output=True)
    assert "scannable=true" in (tmp_path / "output").read_text()
    assert "range_state=security_exempt" not in (tmp_path / "output").read_text()
    assert "config/human-merge-policy.yaml" in (tmp_path / "codex-security-paths.bin").read_text().split("\0")


def test_inline_recovery_has_native_event_admission():
    data = yaml.safe_load((ROOT / ".github/workflows/codex-review-recovery.yml").read_text())
    triggers = data.get("on", data.get(True))
    assert "created" in triggers["pull_request_review_comment"]["types"]
    assert "github.event_name == 'pull_request_review_comment'" in data["jobs"]["recover"]["if"]
    assert "github.actor == 'chatgpt-codex-connector[bot]'" in data["jobs"]["recover"]["if"]
