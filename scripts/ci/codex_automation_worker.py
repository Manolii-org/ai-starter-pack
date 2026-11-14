"""Trusted default-branch controller for subscription automation (assessment).

Runs only from `.github/workflows/codex-automation-worker.yml` on the accepted
`workflow_sha` checkout, after the unprivileged producer completes. Order:

1. ``prepare``: authenticate the exact producer run and inert request
   (`codex_automation_request.admit`) against reviewed controller policy.
2. ``run``: verify explicit automation consent for every role this run needs
   (read-only), append and read back durable custody *before* any model work,
   then execute two isolated guest passes (``assessment``, ``judge``), each
   under the existing per-account session lock and refresh owner, through a
   text-only broker. Emits `automation.inference/v2` receipts per completion.
3. ``publish``: post the captured Judge outcome as an advisory PR comment.

Holds are terminal for the attempt: no retry, no alternate provider, no
fallback to the legacy review lane, and custody is never cleared. Security
roles stay held in the guest; this controller never claims a clean review for
a held role. Source mutation/publication is not part of this controller.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import codex_automation_custody as custody
import codex_automation_receipts as receipts
import codex_automation_request as requests_
import codex_review_profiles as profiles
from codex_review_broker import BrokerError, Subscription, serve
from codex_review_worker import GitHub, Incomplete, docker, git_input

CONFIG = HERE / "codex-automation-worker.json"
ROOT = HERE.parents[1]
MARKER = "<!-- codex-automation-assessment:v1 -->"
MAX_GUEST_OUTPUT = 2 * 1024 * 1024
CLOSURE_DATA = (".ai/security/token-shapes.json", "data/token-shapes.json")
GUEST_MARGIN_SECONDS = 60
MD_UNSAFE = re.compile(r"([\\`*_\[\]()<>#|~!])")
STATES = frozenset({"completed", "held", "blocked_credentials", "blocked_quota", "failed"})


def policy():
    value = json.loads(CONFIG.read_text(encoding="utf-8"))
    roster = value.get("roster")
    if (value.get("schema") != "codex-automation-worker/v1"
            or not isinstance(value.get("allowed_bases"), list) or not value["allowed_bases"]
            or not isinstance(value.get("hold_labels"), list)
            or not isinstance(roster, list) or not roster
            or any(not isinstance(v, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", v) for v in roster)
            or value.get("data_sensitivity") not in receipts.SENSITIVITY
            or value.get("reasoning_effort") not in ("medium", "xhigh")
            or any(type(value.get(k)) is not int or not 0 < value[k] <= limit
                   for k, limit in (("max_requests", 64), ("max_seconds", 1800)))):
        raise Incomplete("controller_policy_invalid")
    return value


def enabled():
    return os.environ.get("CODEX_AUTOMATION_ENABLED") == "true"


def _output(**values):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.writelines(f"{key}={value}\n" for key, value in values.items())


def _state_dir():
    path = Path(os.environ["RUNNER_TEMP"]) / "codex-automation"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def prepare(github, event):
    config = policy()
    if not enabled():
        return None
    target = requests_.admit(github, event, allowed_bases=tuple(config["allowed_bases"]),
                             hold_labels=tuple(config["hold_labels"]))
    (_state_dir() / "target.json").write_text(json.dumps(target, sort_keys=True))
    return target


def context_document(target, config, role, run_id, run_attempt):
    return {
        "schema": "codex-automation-context/v1",
        "repository": target["repository"],
        "pull_request": target["pull_request"],
        "base_sha": target["base_sha"],
        "head_sha": target["head_sha"],
        "role": role,
        "data_sensitivity": config["data_sensitivity"],
        "reasoning_effort": config["reasoning_effort"],
        "controller_run_id": run_id,
        "controller_run_attempt": run_attempt,
        "selected_roles": sorted(config["roster"]),
    }


def build_image(directory):
    """Accepted closure only: trusted scripts and definitions, never candidate Git."""
    context = directory / "image"
    (context / "opt/automation").mkdir(parents=True)
    shutil.copytree(ROOT / "scripts", context / "opt/automation/scripts",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"))
    for name in ("agents", "skills"):
        shutil.copytree(ROOT / ".claude" / name, context / f"opt/automation/.claude/{name}",
                        ignore=shutil.ignore_patterns("__pycache__"))
    # Judge/deep-dive evidence redaction requires the shipped secret-shape catalog;
    # without it every evidence read fails closed and no adjudication is possible.
    catalogs = [path for path in CLOSURE_DATA if (ROOT / path).is_file()]
    if not catalogs:
        raise Incomplete("failed_install")
    for path in catalogs:
        (context / "opt/automation" / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / path, context / "opt/automation" / path)
    shutil.copy(HERE / "codex-automation-runtime/Dockerfile", context / "Dockerfile")
    command, env = docker("build", "--quiet", "--iidfile", str(directory / "image.id"),
                          "--secret", "id=proxy_ca,src="
                          + os.environ.get("CODEX_PROXY_CERT", "/etc/ssl/certs/ca-certificates.crt"),
                          str(context))
    result = subprocess.run(command, env=env, cwd=directory, capture_output=True, timeout=600, check=False)
    image = (directory / "image.id").read_text().strip() if result.returncode == 0 else ""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise Incomplete("failed_install")
    return image


def guest_args(image, name, source, broker_directory, context_file, candidates, value):
    args = ["run", "--name", name, "--rm", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--security-opt", "seccomp=" + str(HERE / "codex-review-runtime/seccomp.json"),
            "--pids-limit", "256", "--memory", "4g", "--cpus", "2",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
            "--tmpfs", f"/state:rw,nosuid,nodev,size=1024m,uid={os.getuid()},gid={os.getgid()},mode=700",
            "--mount", f"type=bind,src={source},dst=/input/repo,readonly",
            "--mount", f"type=bind,src={broker_directory},dst=/broker,readonly",
            "--mount", f"type=bind,src={context_file},dst=/input/codex-automation.json,readonly"]
    if candidates is not None:
        args += ["--mount", f"type=bind,src={candidates},dst=/input/candidates,readonly"]
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        args += ["--env", key + "="]
    for key, item in (("GITHUB_REPOSITORY", value["repository"]), ("PR_NUMBER", value["pull_request"]),
                      ("PR_BASE_SHA", value["base_sha"]), ("PR_HEAD_SHA", value["head_sha"]),
                      ("GITHUB_RUN_ID", value["controller_run_id"]),
                      ("GITHUB_RUN_ATTEMPT", value["controller_run_attempt"]),
                      ("CODEX_AUTOMATION_GUEST_SECONDS", value.get("guest_seconds", 1400))):
        args += ["--env", f"{key}={item}"]
    return [*args, image]


def run_guest(image, source, broker_directory, context_file, candidates, value, *, seconds):
    name = "codex-automation-" + os.urandom(8).hex()
    value = {**value, "guest_seconds": max(60, int(seconds) - 30)}
    command, env = docker(*guest_args(image, name, source, broker_directory, context_file, candidates, value))
    try:
        result = subprocess.run(command, env=env, capture_output=True, timeout=seconds, check=False)
    except subprocess.TimeoutExpired:
        raise Incomplete("failed_scan") from None
    finally:
        cleanup, clean_env = docker("rm", "--force", name)
        subprocess.run(cleanup, env=clean_env, capture_output=True, timeout=20, check=False)
    if len(result.stdout) > MAX_GUEST_OUTPUT:
        raise Incomplete("failed_output")
    lines = result.stdout.decode("utf-8", "replace").strip().splitlines()
    if len(lines) != 1:
        raise Incomplete("failed_output")
    document = json.loads(lines[0])
    if not isinstance(document, dict) or document.get("status") not in ("completed", "held"):
        raise Incomplete("failed_output")
    return document


def write_context(directory, document):
    path = directory / f"context-{document['role']}.json"
    path.write_text(json.dumps(document, sort_keys=True))
    path.chmod(0o444)
    return path


def consent(target, roles):
    """Read-only consent/runner admission for every role before any claim."""
    assignments = {}
    for role in roles:
        assignments[role] = profiles.select_automation(target["repository"], role)
    return assignments


def execute(github, target, directory, *, run_id, run_attempt, workflow_sha, guest=run_guest,
            session=profiles.automation_session, image_builder=build_image, source_loader=git_input):
    """Return (result, receipt_rows). Never retries and never clears custody."""
    config = policy()
    rows = []
    try:
        assignments = consent(target, ("assessment", "judge"))
    except profiles.ProfileError as exc:
        return {"state": "blocked_credentials", "reason": str(exc)}, rows
    try:
        custody.claim(github, {**target, "operation": "assessment"}, producer_run_id=target["request_run_id"],
                      workflow_sha=workflow_sha, enabled=True)
    except custody.CustodyHold as exc:
        return {"state": "held", "reason": str(exc)}, rows
    passes = {}
    unmeasured = {}
    try:
        source, paths = source_loader(directory, {**target, "lane": "code"}, github.token)
        if not paths:
            return {"state": "completed", "reason": "empty_diff", "publication": []}, rows
        image = image_builder(directory)
        candidates = None
        for role in ("assessment", "judge"):
            document = context_document(target, config, role, run_id, run_attempt)
            context_file = write_context(directory, document)
            broker_dir = directory / f"broker-{role}"
            broker_dir.mkdir(mode=0o700)
            audit = None
            try:
                with session(assignments[role], target["repository"], role) as (auth, _snapshot), \
                        serve(broker_dir / "model.sock", Subscription(auth), text_only=True,
                              max_requests=config["max_requests"], max_seconds=config["max_seconds"],
                              expected_effort=config["reasoning_effort"]) as broker:
                    try:
                        output = guest(image if image else "", source, broker_dir, context_file, candidates,
                                       document, seconds=config["max_seconds"] - GUEST_MARGIN_SECONDS)
                    finally:
                        # Spent completions are measured even when the guest fails.
                        audit = broker.audit()
            finally:
                if audit is not None:
                    unmeasured[role] = max(0, audit.get("requests", 0) - len(audit.get("text_completions", [])))
                    rows += receipts.build(
                        audit.get("text_completions", []), repository=target["repository"], run_id=run_id,
                        run_attempt=run_attempt, base_sha=target["base_sha"], head_sha=target["head_sha"],
                        workflow_sha=workflow_sha, role=role, data_sensitivity=config["data_sensitivity"],
                        correlation_id=f"{target['repository']}#{target['pull_request']}@{target['head_sha']}/{role}")
            if broker.terminal_error:
                return {"state": "blocked_quota" if broker.terminal_error == "subscription_quota_exhausted"
                        else "failed", "reason": broker.terminal_error, "passes": passes}, rows
            passes[role] = output
            if output["status"] != "completed":
                return {"state": "held", "reason": output.get("reason", "guest_held"), "passes": passes}, rows
            if role == "assessment":
                candidates = directory / "candidates"
                candidates.mkdir(mode=0o700)
                for name, body in output["candidates"].items():
                    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}\.json", name):
                        return {"state": "failed", "reason": "candidate_name_invalid", "passes": passes}, rows
                    (candidates / name).write_text(json.dumps(body))
        return {"state": "completed", "reason": "adjudicated", "passes": passes,
                "unmeasured_requests": unmeasured,
                "publication": passes["judge"].get("publication", [])}, rows
    except profiles.ProfileError as exc:
        return {"state": "blocked_quota" if str(exc) in {"profile_capacity_exhausted", "profile_session_busy"}
                else "blocked_credentials", "reason": str(exc), "passes": passes}, rows
    except (Incomplete, BrokerError, OSError, ValueError, subprocess.SubprocessError,
            receipts.ReceiptError) as exc:
        return {"state": "failed", "reason": str(exc)[:200] or type(exc).__name__, "passes": passes}, rows


def _safe(value, limit):
    """Neutralise Markdown and @mentions in model/PR-derived text (as the Judge does)."""
    text = " ".join(str(value).split())[:limit]
    return MD_UNSAFE.sub(r"\\\1", text).replace("@", "@\u200b")


def comment_body(target, result):
    event_names = {"APPROVE", "COMMENT", "REQUEST_CHANGES"}
    lines = [f"{MARKER}\n<!-- head={target['head_sha']} -->", "### Subscription automation assessment (advisory)", "",
             f"Head {target['head_sha']} · state {_safe(result['state'], 40)} · reason {_safe(result.get('reason', ''), 120)}", ""]
    for item in result.get("publication", []):
        if item.get("action") == "review":
            event = item.get("event") if item.get("event") in event_names else "UNKNOWN"
            lines.append(f"**Judge verdict:** {event} ({len(item.get('surviving') or [])} finding(s))")
            for finding in (item.get("surviving") or [])[:20]:
                where = _safe(f"{finding.get('file', '?')}:{finding.get('line', '?')}", 200)
                lines.append(f"- {where} {_safe(finding.get('message', ''), 300)}")
        elif item.get("action") == "advisory":
            lines.append(f"**Advisory:** {_safe(item.get('detail', ''), 500)}")
        elif item.get("action") == "no_findings":
            lines.append("**Judge verdict:** no surviving findings")
    lines += ["", "_Advisory only; never a required check. Held roles are missing coverage, not a pass._"]
    return "\n".join(lines)


def main(argv):
    command = argv[1] if len(argv) > 1 else ""
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    github = GitHub(repository, os.environ.get("GH_TOKEN", ""))
    state = _state_dir()
    if command == "prepare":
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        try:
            target = prepare(github, event)
        except (requests_.RequestRefused, Incomplete) as exc:
            print(f"refused: {exc}")
            _output(needed="false")
            return 0
        _output(needed="true" if target else "false")
        return 0
    target = json.loads((state / "target.json").read_text(encoding="utf-8"))
    if command == "run":
        with tempfile.TemporaryDirectory(dir=state) as tmp:
            result, rows = execute(github, target, Path(tmp), run_id=int(os.environ["GITHUB_RUN_ID"]),
                                   run_attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]),
                                   workflow_sha=os.environ["GITHUB_WORKFLOW_SHA"])
        if result["state"] not in STATES:
            result = {"state": "failed", "reason": "unknown_state"}
        out = state / "result"
        out.mkdir(exist_ok=True)
        (out / "result.json").write_text(json.dumps(result, sort_keys=True))
        (out / "inference-receipts.jsonl").write_text(receipts.dumps(rows))
        _output(artifact=f"codex-automation-result-{target['pull_request']}-{target['head_sha']}",
                state=result["state"])
        return 0
    if command == "publish":
        result = json.loads((state / "result/result.json").read_text(encoding="utf-8"))
        number = int(target["pull_request"])
        pr = github.json(f"/pulls/{number}")
        if not isinstance(pr, dict) or pr.get("state") != "open" or (pr.get("head") or {}).get("sha") != target["head_sha"]:
            print("superseded: PR head changed or closed; advisory not posted")
            return 0
        head_marker = f"<!-- head={target['head_sha']} -->"
        existing = github.pages(f"/issues/{number}/comments")
        if any(isinstance(c, dict) and (c.get("user") or {}).get("id") == custody.BOT_ID
               and MARKER in (c.get("body") or "") and head_marker in (c.get("body") or "") for c in existing):
            print("already published for this head")
            return 0
        github.json(f"/issues/{number}/comments", method="POST", data={"body": comment_body(target, result)})
        return 0
    raise SystemExit("usage: codex_automation_worker.py prepare|run|publish")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
