"""Execute the actual native/coverage/recovery workflow programs.

Legacy credential-bearing PR scan tests moved to test_codex_review_worker.py,
which exercises the replacement trusted controller and publisher.
"""

import json
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_all_workflow_shell_blocks_parse():
    for path in (ROOT / ".github/workflows").glob("codex-*.yml"):
        data = yaml.safe_load(path.read_text())
        for job in data["jobs"].values():
            for step in job.get("steps", []):
                if "run" in step:
                    result = subprocess.run(
                        ["bash", "-n"],
                        input=step["run"],
                        text=True,
                        capture_output=True,
                        timeout=5,
                    )
                    assert result.returncode == 0, (
                        path.name,
                        step.get("name"),
                        result.stderr,
                    )


@pytest.mark.parametrize(
    "lane,cli,native,actor,stale,passed",
    [
        ("code", "", "formal", "chatgpt-codex-connector[bot]", False, True),
        ("code", "", "inline", "chatgpt-codex-connector[bot]", False, True),
        (
            "code",
            "blocked_quota",
            "inline",
            "chatgpt-codex-connector[bot]",
            False,
            True,
        ),
        ("code", "", "inline_reply", "chatgpt-codex-connector[bot]", False, False),
        ("code", "", "inline_no_review", "chatgpt-codex-connector[bot]", False, False),
        ("code", "", "inline_stale", "chatgpt-codex-connector[bot]", False, False),
        (
            "code",
            "",
            "inline_remapped_stale",
            "chatgpt-codex-connector[bot]",
            False,
            False,
        ),
        (
            "code",
            "",
            "inline_parent_pending",
            "chatgpt-codex-connector[bot]",
            False,
            False,
        ),
        (
            "code",
            "",
            "inline_missing_original",
            "chatgpt-codex-connector[bot]",
            False,
            False,
        ),
        ("code", "", "inline_parent_old", "chatgpt-codex-connector[bot]", False, False),
        ("code", "", "inline", "github-actions[bot]", False, False),
        ("code", "", "security_inline", "chatgpt-codex-connector[bot]", False, False),
        ("security", "", "inline", "chatgpt-codex-connector[bot]", False, False),
        ("code", "", "inline_unmarked", "chatgpt-codex-connector[bot]", False, False),
        ("code", "", "quota_summary", "chatgpt-codex-connector[bot]", False, False),
        (
            "code",
            "",
            "misleading_summary",
            "chatgpt-codex-connector[bot]",
            False,
            False,
        ),
        ("security", "", "mixed_summary", "chatgpt-codex-connector[bot]", False, True),
        ("code", "", "mixed_summary", "chatgpt-codex-connector[bot]", False, False),
        (
            "security",
            "",
            "ambiguous_summary",
            "chatgpt-codex-connector[bot]",
            False,
            False,
        ),
        ("code", "", None, "chatgpt-codex-connector[bot]", False, False),
        ("security", "", "security", "chatgpt-codex-connector[bot]", False, True),
        ("code", "completed", None, "chatgpt-codex-connector[bot]", False, False),
        (
            "code",
            "blocked_quota",
            "formal",
            "chatgpt-codex-connector[bot]",
            False,
            True,
        ),
        (
            "code",
            "blocked_quota",
            "security",
            "chatgpt-codex-connector[bot]",
            False,
            False,
        ),
        ("code", "blocked_quota", None, "chatgpt-codex-connector[bot]", False, False),
        ("code", "blocked_quota", "formal", "github-actions[bot]", False, False),
        ("code", "completed", None, "chatgpt-codex-connector[bot]", True, False),
        (
            "security",
            "blocked_quota",
            "security",
            "chatgpt-codex-connector[bot]",
            False,
            True,
        ),
        (
            "security",
            "blocked_quota",
            "formal",
            "chatgpt-codex-connector[bot]",
            False,
            False,
        ),
    ],
)
def test_actual_completion_job_native_fallback(lane, cli, native, actor, stale, passed):
    workflow = (
        "codex-pr-review.yml" if lane == "code" else "codex-security-diff-scan.yml"
    )
    data = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    script = data["jobs"]["completion"]["steps"][0]["with"]["script"]
    runner = r"""
const fs=require('fs'),p=JSON.parse(fs.readFileSync(0,'utf8'));
const sha='a'.repeat(40), messages=[], failures=[];
const item={user:{login:p.actor},body:'Codex Review',commit_id:sha,state:'COMMENTED'};
const bodies={security:'| 🔒 **Security Review** | ✅ **Completed** | `aaaaaaaaaa` |',
quota_summary:'| 📝 **Code Review** | ⚠️ **Usage limits reached** | `aaaaaaaaaa` |',
misleading_summary:'| 📝 **Code Review** | ⚠️ **Usage limits reached** | `aaaaaaaaaa` | ✅ **Completed** |',
mixed_summary:'| 📝 **Code Review** | ⚠️ **Usage limits reached** | `aaaaaaaaaa` |\n| 🔒 **Security Review** | ✅ **Completed** | `aaaaaaaaaa` |',
ambiguous_summary:'| 🔒 **Security Review** | ✅ **Completed** | `aaaaaaaaaa` |'};
const comments=bodies[p.native]?[{user:{login:p.actor},body:bodies[p.native]}]:[];
const api={repos:{getCommit:async()=>{if(p.native==='ambiguous_summary')throw Error('422 ambiguous');return {data:{sha}};}},pulls:{get:async()=>({data:{head:{sha:p.stale?'b'.repeat(40):sha}}}),listReviews:async()=>({data:p.native==='formal'?[item]:(String(p.native).startsWith('inline')||p.native==='security_inline')?[{...item,id:123,body:'',commit_id:['inline_remapped_stale','inline_parent_old'].includes(p.native)?'b'.repeat(40):sha,state:p.native==='inline_parent_pending'?'PENDING':'COMMENTED'}]:[]}),listReviewComments:async()=>({data:['inline','inline_stale','inline_remapped_stale','inline_parent_pending','inline_missing_original','inline_parent_old','security_inline','inline_unmarked','inline_reply','inline_no_review'].includes(p.native)?[{user:{login:p.actor},commit_id:p.native==='inline_stale'?'b'.repeat(40):sha,original_commit_id:p.native==='inline_missing_original'?undefined:p.native==='inline_remapped_stale'?'b'.repeat(40):sha,path:'src/file.ts',pull_request_review_id:p.native==='inline_no_review'?null:123,in_reply_to_id:p.native==='inline_reply'?44:null,body:p.native==='inline_unmarked'?'arbitrary text':'a finding\n<!-- codex-'+(p.native==='security_inline'?'security':'code')+'-review-finding:v1 -->'}]:[]})},issues:{listComments:async()=>({data:comments})}};
const github={rest:api,paginate:async fn=>(await fn()).data};
const core={info:s=>messages.push(s),setFailed:s=>failures.push(s)};
const context={repo:{owner:'example',repo:'project'},payload:{pull_request:{number:1}}};
const proc={env:{EXPECTED_HEAD:sha,REVIEW_HEAD:sha,REVIEW_STATE:p.cli}};
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
new AsyncFunction('github','core','context','process',p.script)(github,core,context,proc).then(()=>process.stdout.write(JSON.stringify({messages,failures}))).catch(e=>{console.error(e);process.exit(1);});
"""
    result = subprocess.run(
        ["node", "-e", runner],
        input=json.dumps(
            {
                "script": script,
                "native": native,
                "actor": actor,
                "stale": stale,
                "cli": cli,
            }
        ),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert (not json.loads(result.stdout)["failures"]) == passed


@pytest.mark.parametrize(
    "body,expected",
    [
        (
            "Codex review: Didn’t find any major issues.\nreviewed commit: aaaaaaaaaa",
            ["codex-pr-review.yml"],
        ),
        (
            "| 📝 **Code Review** | ✅ **Completed** | `aaaaaaaaaa` |",
            ["codex-pr-review.yml"],
        ),
        (
            "| 🔒 **Security Review** | ✅ **Completed** | `aaaaaaaaaa` |",
            ["codex-security-diff-scan.yml"],
        ),
        (
            "| 📝 **Code Review** | ✅ **Completed** | `aaaaaaaaaa` |\n| 🔒 **Security Review** | ✅ **Completed** | `aaaaaaaaaa` |",
            ["codex-pr-review.yml", "codex-security-diff-scan.yml"],
        ),
        ("inline", ["codex-pr-review.yml"]),
        ("inline_stale", []),
        ("inline_remapped_stale", []),
        ("inline_parent_pending", []),
        ("inline_parent_old", []),
        ("inline_remapped_stale_formaltext", []),
        ("formal_current", ["codex-pr-review.yml"]),
        ("formal_pending", []),
        ("formal_missing_state", []),
        ("quota_inline", []),
        ("security_inline", []),
        ("You have reached your Codex usage limits for code reviews.", 0),
        ("Codex review: Didn’t find any major issues.\nReviewed commit: bbbbbbbbbb", 0),
    ],
)
def test_actual_recovery_reruns_only_completion_jobs(body, expected):
    data = yaml.safe_load(
        (ROOT / ".github/workflows/codex-review-recovery.yml").read_text()
    )
    script = data["jobs"]["recover"]["steps"][0]["with"]["script"]
    runner = r"""
const fs=require('fs'),p=JSON.parse(fs.readFileSync(0,'utf8')),sha='a'.repeat(40),rerun=[];
const pull={state:'open',draft:false,head:{sha}};
const github={rest:{pulls:{get:async()=>({data:pull}),getReview:async()=>({data:{commit_id:['inline_remapped_stale','inline_remapped_stale_formaltext','inline_parent_old'].includes(p.body)?'b'.repeat(40):sha,user:{login:'chatgpt-codex-connector[bot]'},state:p.body==='inline_parent_pending'?'PENDING':'COMMENTED'}})},actions:{
listWorkflowRuns:async args=>({data:{workflow_runs:[{id:'other-pr',head_sha:sha,path:'.github/workflows/'+args.workflow_id,conclusion:'failure',pull_requests:[{number:99}]},{id:args.workflow_id,head_sha:sha,path:'.github/workflows/'+args.workflow_id,conclusion:'failure',pull_requests:[{number:1}]}]}}),
listJobsForWorkflowRun:async args=>({data:[{id:1,name:'Codex review (subscription)',conclusion:'failure'},{id:args.run_id,name:args.run_id==='codex-pr-review.yml'?'Codex review coverage':'Codex security coverage',conclusion:'failure'}]}),
reRunJobForWorkflowRun:async args=>rerun.push(args.job_id)
}},paginate:async(fn,args)=>(await fn(args)).data};
const context={repo:{owner:'example',repo:'project'},payload:{issue:{number:1,pull_request:{url:'https://api.github.com/repos/example/project/pulls/1'}},comment:{body:p.body,commit_id:['inline','inline_parent_old','inline_remapped_stale','inline_remapped_stale_formaltext','inline_parent_pending','quota_inline'].includes(p.body)?sha:'b'.repeat(40),original_commit_id:['inline_remapped_stale','inline_remapped_stale_formaltext'].includes(p.body)?'b'.repeat(40):sha,path:'src/file.ts',pull_request_review_id:123}}};
if(['inline','inline_parent_old','inline_stale','inline_remapped_stale','inline_remapped_stale_formaltext','inline_parent_pending','security_inline','quota_inline'].includes(p.body)){context.payload.pull_request={number:1};delete context.payload.issue;}
if(['inline','inline_parent_old','inline_stale','inline_remapped_stale','inline_remapped_stale_formaltext','inline_parent_pending','security_inline','quota_inline'].includes(p.body))context.payload.comment.body='a finding\n<!-- codex-'+(p.body==='security_inline'?'security':'code')+'-review-finding:v1 -->';
if(p.body==='quota_inline')context.payload.comment.body='You have reached your Codex usage limits for code reviews.\n<!-- codex-code-review-finding:v1 -->';
if(p.body==='inline_remapped_stale_formaltext')context.payload.comment.body='Codex review described an old finding\n<!-- codex-code-review-finding:v1 -->';
if(String(p.body).startsWith('formal_'))context.payload={pull_request:{number:1},review:{body:'Codex review',commit_id:sha,state:p.body==='formal_pending'?'PENDING':p.body==='formal_missing_state'?undefined:'COMMENTED'}};
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
new AsyncFunction('github','core','context',p.script)(github,{info:()=>{}},context).then(()=>process.stdout.write(JSON.stringify(rerun))).catch(e=>{console.error(e);process.exit(1);});
"""
    result = subprocess.run(
        ["node", "-e", runner],
        input=json.dumps({"script": script, "body": body}),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == (expected if isinstance(expected, list) else [])


def test_inline_recovery_has_native_event_admission():
    data = yaml.safe_load(
        (ROOT / ".github/workflows/codex-review-recovery.yml").read_text()
    )
    triggers = data.get("on", data.get(True))
    assert "created" in triggers["pull_request_review_comment"]["types"]
    assert (
        "github.event_name == 'pull_request_review_comment'"
        in data["jobs"]["recover"]["if"]
    )
    assert (
        "github.actor == 'chatgpt-codex-connector[bot]'"
        in data["jobs"]["recover"]["if"]
    )
