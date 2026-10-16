"""Execute receipt and publisher preparation from the actual workflow definitions."""
import json
import os
import re
import subprocess
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
const comments=p.native==='security'?[{user:{login:p.actor},body:'| 🔒 **Security Review** | ✅ **Completed** | `aaaaaaaaaa` |'}]:[];
const api={pulls:{get:async()=>({data:{head:{sha:p.stale?'b'.repeat(40):sha}}}),listReviews:async()=>({data:p.native==='formal'?[item]:[]}),listReviewComments:async()=>({data:[]})},issues:{listComments:async()=>({data:comments})}};
const github={rest:api,paginate:async fn=>(await fn()).data};
const core={info:s=>messages.push(s),setFailed:s=>failures.push(s)};
const context={repo:{owner:'example',repo:'project'},payload:{pull_request:{number:1}}};
const proc={env:{EXPECTED_HEAD:sha,REVIEW_HEAD:sha,REVIEW_STATE:p.cli}};
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
new AsyncFunction('github','core','context','process',p.script)(github,core,context,proc).then(()=>process.stdout.write(JSON.stringify({messages,failures}))).catch(e=>{console.error(e);process.exit(1);});
'''
    result = subprocess.run(['node', '-e', runner], input=json.dumps({'script':script,'native':native,'actor':actor,'stale':stale,'cli':cli}), capture_output=True, text=True, check=True, timeout=10)
    assert (not json.loads(result.stdout)['failures']) == passed


def test_actual_recovery_reruns_only_completion_jobs():
    data = yaml.safe_load((ROOT / '.github/workflows/codex-review-recovery.yml').read_text())
    script = data['jobs']['recover']['steps'][0]['with']['script']
    runner = r'''
const fs=require('fs'),p=JSON.parse(fs.readFileSync(0,'utf8')),sha='a'.repeat(40),rerun=[];
const pull={state:'open',draft:false,head:{sha}};
const github={rest:{pulls:{get:async()=>({data:pull})},actions:{
listWorkflowRuns:async args=>({data:{workflow_runs:[{id:args.workflow_id,head_sha:sha,path:'.github/workflows/'+args.workflow_id,conclusion:'failure'}]}}),
listJobsForWorkflowRun:async args=>({data:[{id:1,name:'Codex review (subscription)',conclusion:'failure'},{id:args.run_id,name:args.run_id==='codex-pr-review.yml'?'Codex review coverage':'Codex security coverage',conclusion:'failure'}]}),
reRunJobForWorkflowRun:async args=>rerun.push(args.job_id)
}},paginate:async(fn,args)=>(await fn(args)).data};
const context={repo:{owner:'example',repo:'project'},payload:{issue:{number:1}}};
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
new AsyncFunction('github','core','context',p.script)(github,{info:()=>{}},context).then(()=>process.stdout.write(JSON.stringify(rerun))).catch(e=>{console.error(e);process.exit(1);});
'''
    result = subprocess.run(['node', '-e', runner], input=json.dumps({'script':script}), capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(result.stdout) == ['codex-pr-review.yml','codex-security-diff-scan.yml']


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
    record = record.replace('${{ runner.temp }}', str(tmp_path))
    subprocess.run(['bash', '-c', record], cwd=ROOT, env=env, capture_output=True, text=True, check=True, timeout=10)
    assert json.loads((tmp_path / 'codex-security-receipt.json').read_text())['state'] == expected
