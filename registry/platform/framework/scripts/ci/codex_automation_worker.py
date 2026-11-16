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
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import codex_automation_custody as custody
import codex_automation_hosted as hosted
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
# The execute job has a 60-minute limit; keep setup and retention headroom.
EXECUTE_SECONDS = 52 * 60
SESSION_WAIT_SECONDS = 1250
MIN_GUEST_SECONDS = 120
MD_UNSAFE = re.compile(r"([\\`*_\[\]()<>#|~!])")
STATES = frozenset({"completed", "held", "blocked_credentials", "blocked_quota", "failed"})
MAX_PR_TITLE_BYTES = 1024
MAX_PR_BODY_BYTES = 64 * 1024


def _policy_path():
    """Hosted consumers keep their policy in their own reviewed default branch;
    the reusable controller passes that trusted checkout's path."""
    override = os.environ.get("CODEX_AUTOMATION_POLICY", "")
    if hosted.enabled() and override:
        path = Path(override)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise Incomplete("controller_policy_invalid")
        return path
    return CONFIG


def policy():
    value = json.loads(_policy_path().read_text(encoding="utf-8"))
    roster = value.get("roster")
    if (value.get("schema") != "codex-automation-worker/v1"
            or not isinstance(value.get("allowed_bases"), list) or not value["allowed_bases"]
            or not isinstance(value.get("hold_labels"), list)
            or not isinstance(roster, list) or not roster
            or any(not isinstance(v, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", v) for v in roster)
            # The Judge always submits "internal" input; a "public" policy would make
            # the adapter refuse every Judge turn as a scope mismatch.
            or value.get("data_sensitivity") != "internal"
            or value.get("reasoning_effort") not in ("medium", "xhigh")
            or type(value.get("autofix", False)) is not bool
            or type(value.get("max_autofix_rounds", 1)) is not int
            or not 1 <= value.get("max_autofix_rounds", 1) <= 4
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
    (_state_dir() / "pr-meta.json").write_text(json.dumps(pr_metadata(github, target), sort_keys=True,
                                                         ensure_ascii=False), encoding="utf-8")
    return target


_UNSAFE_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _bounded(text, limit):
    """UTF-8-bounded text safe for argv/env: unpaired surrogates become "?"
    and NUL/C0 controls (except tab/newline/CR) are removed."""
    if not isinstance(text, str):
        return ""
    text = _UNSAFE_CONTROL.sub("", text.encode("utf-8", "replace").decode("utf-8"))
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", "ignore") + "\n[truncated]"


def pr_metadata(github, target):
    """Title/body of the admitted head (untrusted data for scope comparison).

    Read at admission and bound to the admitted head; the guest receives it as a
    read-only file, never as instructions, and the stage runners wrap it as
    untrusted PR metadata."""
    pr = github.json(f"/pulls/{int(target['pull_request'])}")
    if not isinstance(pr, dict) or (pr.get("head") or {}).get("sha") != target["head_sha"]:
        raise requests_.RequestRefused("request_superseded_revision")
    return {"title": _bounded(pr.get("title") or "", MAX_PR_TITLE_BYTES),
            "body": _bounded(pr.get("body") or "", MAX_PR_BODY_BYTES)}


def context_document(target, config, role, run_id, run_attempt, roles=None):
    """`selected_roles` is the roster for assessment; for the judge it is exactly
    the roles the assessment pass dispatched, which the Judge requires to be
    present (an undispatched roster role would count as missing evidence)."""
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
        "selected_roles": sorted(config["roster"] if roles is None else roles),
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
    # Argument list without a shell; CODEX_PROXY_CERT is the trusted job's own build-secret path.
    result = subprocess.run(command, env=env, cwd=directory, capture_output=True, timeout=600, check=False)  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
    image = (directory / "image.id").read_text().strip() if result.returncode == 0 else ""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        _report_build_failure(result)
        raise Incomplete("failed_install")
    return image


def _report_build_failure(result, limit=40):
    """Leave the guest-image build failure in the trusted job log.

    The build context is the accepted closure only (no candidate source and no
    credential), so its output is safe to show; without it ``failed_install``
    names no cause.
    """
    text = (result.stderr or b"").decode("utf-8", "replace") + (result.stdout or b"").decode("utf-8", "replace")
    tail = [line for line in text.splitlines() if line.strip()][-limit:]
    print(f"guest image build failed (exit {result.returncode}):", file=sys.stderr)
    for line in tail:
        print("  " + line[:400], file=sys.stderr)


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
    meta = Path(context_file).with_name("pr-meta.json")
    if meta.is_file():
        args += ["--mount", f"type=bind,src={meta},dst=/input/pr-meta.json,readonly"]
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
    config = policy() if hosted.enabled() else None
    for role in roles:
        if config is not None:
            assignments[role] = hosted.select(target["repository"], role, config)
        else:
            assignments[role] = profiles.select_automation(target["repository"], role)
    return assignments


def _wait_budget(deadline):
    """Lock wait that still leaves a guest pass inside the job deadline."""
    remaining = deadline - time.monotonic() - MIN_GUEST_SECONDS - GUEST_MARGIN_SECONDS
    if remaining < 1:
        raise Incomplete("controller_time_budget_exhausted")
    return int(min(SESSION_WAIT_SECONDS, remaining))


def _guest_budget(deadline, config):
    remaining = deadline - time.monotonic() - GUEST_MARGIN_SECONDS
    if remaining < MIN_GUEST_SECONDS:
        raise Incomplete("controller_time_budget_exhausted")
    return int(min(config["max_seconds"] - GUEST_MARGIN_SECONDS, remaining))


def _checkpoint(path, rows):
    """Persist receipts after every pass so a cancelled job keeps them."""
    if path is not None:
        path.write_text(receipts.dumps(rows))


def write_pr_meta(directory, meta):
    meta = meta or {}
    value = {"title": _bounded(meta.get("title", ""), MAX_PR_TITLE_BYTES),
             "body": _bounded(meta.get("body", ""), MAX_PR_BODY_BYTES)}
    path = directory / "pr-meta.json"
    # Raw UTF-8 (not \uXXXX escapes) keeps the file within the guest's bound.
    path.write_text(json.dumps(value, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o444)
    return path


def execute(github, target, directory, *, run_id, run_attempt, workflow_sha, guest=run_guest,
            session=None, image_builder=build_image, source_loader=git_input,
            pr_meta=None, deadline=None, checkpoint=None):
    """Return (result, receipt_rows). Never retries and never clears custody."""
    config = policy()
    if session is None:
        session = hosted.session if hosted.enabled() else profiles.automation_session
    rows = []
    try:
        assignments = consent(target, ("assessment", "judge"))
    except profiles.ProfileError as exc:
        return {"state": "blocked_credentials", "reason": str(exc)}, rows
    try:
        # custody authenticates the running controller (this workflow_run job),
        # not the completed producer run already authenticated by admission.
        custody.claim(github, {**target, "operation": "assessment"}, producer_run_id=run_id,
                      workflow_sha=workflow_sha, enabled=True)
    except custody.CustodyHold as exc:
        return {"state": "held", "reason": str(exc)}, rows
    unmeasured = {}
    deadline = time.monotonic() + EXECUTE_SECONDS if deadline is None else deadline
    result = _passes(github, target, directory, config, rows, unmeasured, assignments, run_id=run_id,
                     run_attempt=run_attempt, workflow_sha=workflow_sha, guest=guest, session=session,
                     image_builder=image_builder, source_loader=source_loader, pr_meta=pr_meta,
                     deadline=deadline, checkpoint=checkpoint)
    # Every exit after broker execution keeps the unknown-outcome accounting,
    # not only the adjudicated one (failed/held/blocked passes most need it).
    result["unmeasured_requests"] = unmeasured
    return result, rows


def _passes(github, target, directory, config, rows, unmeasured, assignments, *, run_id, run_attempt, workflow_sha,
            guest, session, image_builder, source_loader, pr_meta, deadline, checkpoint):
    passes = {}
    try:
        write_pr_meta(directory, pr_meta)
        source, paths = source_loader(directory, {**target, "lane": "code"}, github.token)
        if not paths:
            return {"state": "completed", "reason": "empty_diff", "publication": []}
        image = image_builder(directory)
        candidates = None
        dispatched = None
        for role in ("assessment", "judge"):
            document = context_document(target, config, role, run_id, run_attempt, roles=dispatched)
            context_file = write_context(directory, document)
            broker_dir = directory / f"broker-{role}"
            broker_dir.mkdir(mode=0o700)
            audit, broker = None, None
            try:
                with session(assignments[role], target["repository"], role,
                             wait_seconds=_wait_budget(deadline)) as (auth, _snapshot), \
                        serve(broker_dir / "model.sock", Subscription(auth), text_only=True,
                              max_requests=config["max_requests"], max_seconds=config["max_seconds"],
                              expected_effort=config["reasoning_effort"]) as broker:
                    output = guest(image if image else "", source, broker_dir, context_file, candidates,
                                   document, seconds=_guest_budget(deadline, config))
            finally:
                # Audit after the broker has shut down, so a completion recorded
                # while it stopped is measured; spent completions are measured
                # even when the guest fails.
                audit = broker.audit() if broker is not None else None
                if audit is not None:
                    unmeasured[role] = max(0, audit.get("requests", 0) - len(audit.get("text_completions", [])))
                    rows += receipts.build(
                        audit.get("text_completions", []), repository=target["repository"], run_id=run_id,
                        run_attempt=run_attempt, base_sha=target["base_sha"], head_sha=target["head_sha"],
                        workflow_sha=workflow_sha, role=role, data_sensitivity=config["data_sensitivity"],
                        correlation_id=f"{target['repository']}#{target['pull_request']}@{target['head_sha']}/{role}")
                    _checkpoint(checkpoint, rows)
            if broker.terminal_error:
                return {"state": "blocked_quota" if broker.terminal_error == "subscription_quota_exhausted"
                        else "failed", "reason": broker.terminal_error, "passes": passes}
            passes[role] = output
            if output["status"] != "completed":
                return {"state": "held", "reason": output.get("reason", "guest_held"), "passes": passes}
            if role == "assessment":
                ran = output.get("ran") or {}
                dispatched = sorted({name for kind in ("skills", "agents") for name in (ran.get(kind) or [])})
                if any(not isinstance(n, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", n) for n in dispatched) \
                        or len(dispatched) > 100:
                    return {"state": "failed", "reason": "dispatched_roles_invalid", "passes": passes}
                candidates = directory / "candidates"
                candidates.mkdir(mode=0o700)
                for name, body in output["candidates"].items():
                    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}\.json", name):
                        return {"state": "failed", "reason": "candidate_name_invalid", "passes": passes}
                    (candidates / name).write_text(json.dumps(body))
        result = {"state": "completed", "reason": "adjudicated", "passes": passes,
                  "unmeasured_requests": unmeasured,
                  "publication": passes["judge"].get("publication", [])}
        if config.get("autofix") is True:
            try:
                result["autofix"] = autofix_stage(github, target, directory, config, result["publication"], image,
                                                  source, run_id=run_id, run_attempt=run_attempt,
                                                  workflow_sha=workflow_sha, guest=guest, session=session,
                                                  rows=rows, unmeasured=unmeasured, deadline=deadline,
                                                  checkpoint=checkpoint)
            except Exception as exc:  # noqa: BLE001 - the finished Judge outcome must survive
                result["autofix"] = {"outcome": "failed", "reason": type(exc).__name__}
        return result
    except profiles.ProfileError as exc:
        return {"state": "blocked_quota" if str(exc) in {"profile_capacity_exhausted", "profile_session_busy"}
                else "blocked_credentials", "reason": str(exc), "passes": passes}
    except (Incomplete, BrokerError, OSError, ValueError, subprocess.SubprocessError,
            receipts.ReceiptError) as exc:
        return {"state": "failed", "reason": str(exc)[:200] or type(exc).__name__, "passes": passes}


def _safe(value, limit):
    """Neutralise Markdown and @mentions in model/PR-derived text (as the Judge does)."""
    text = " ".join(str(value).split())[:limit]
    return MD_UNSAFE.sub(r"\\\1", text).replace("@", "@\u200b")


def surviving_findings(publication):
    """Only a captured Judge review's surviving findings may be repaired."""
    for item in publication or []:
        if item.get("action") == "review" and item.get("event") in {"COMMENT", "REQUEST_CHANGES"}:
            rows = [f for f in (item.get("surviving") or []) if isinstance(f, dict)
                    and isinstance(f.get("file"), str) and type(f.get("line")) is int]
            return [{k: f.get(k) for k in ("file", "line", "severity", "message", "fix", "finding_id")}
                    for f in rows][:20]
    return []


def autofix_stage(github, target, directory, config, publication, image, source, *, run_id, run_attempt,
                  workflow_sha, guest, session, rows, unmeasured, deadline=None, checkpoint=None):
    """Editor half only: produce an inert publisher packet. No publication
    credential exists in this job; `autofix-publish.py` decides separately."""
    findings = surviving_findings(publication)
    if not findings:
        return {"outcome": "nothing_to_fix"}
    if os.environ.get("CODEX_AUTOMATION_PUBLISH") != "true":
        # Never spend quota or burn a head's custody on a fix nothing can publish.
        return {"outcome": "publication_disabled"}
    rounds = prior_autofix_rounds(github, target)
    if rounds is None:
        return {"outcome": "held", "reason": "autofix_history_unverified"}
    if rounds >= config.get("max_autofix_rounds", 1):
        return {"outcome": "round_cap_reached", "rounds": rounds}
    admitted = editor_admission(target)
    if admitted is not True:
        return {"outcome": "held", "reason": admitted}
    try:
        assignment = consent(target, ("autofix",))["autofix"]
    except profiles.ProfileError as exc:
        return {"outcome": "blocked_credentials", "reason": str(exc)}
    try:
        custody.claim(github, {**target, "operation": "autofix"}, producer_run_id=run_id,
                      workflow_sha=workflow_sha, enabled=True)
    except custody.CustodyHold as exc:
        return {"outcome": "held", "reason": str(exc)}
    handoff = directory / "autofix-input"
    handoff.mkdir(mode=0o700)
    (handoff / "autofix-findings.json").write_text(json.dumps(findings))
    document = context_document(target, config, "autofix", run_id, run_attempt, roles=[])
    context_file = write_context(directory, document)
    broker_dir = directory / "broker-autofix"
    broker_dir.mkdir(mode=0o700)
    audit, output, broker = None, None, None
    deadline = time.monotonic() + EXECUTE_SECONDS if deadline is None else deadline
    try:
        with session(assignment, target["repository"], "autofix",
                     wait_seconds=_wait_budget(deadline)) as (auth, _snapshot), \
                serve(broker_dir / "model.sock", Subscription(auth), text_only=True, max_requests=2,
                      max_seconds=config["max_seconds"], expected_effort=config["reasoning_effort"]) as broker:
            output = guest(image, source, broker_dir, context_file, handoff, document,
                           seconds=_guest_budget(deadline, config))
    except (Incomplete, BrokerError, OSError, ValueError, subprocess.SubprocessError, profiles.ProfileError) as exc:
        return {"outcome": "failed", "reason": str(exc)[:200] or type(exc).__name__}
    finally:
        # Audit after broker shutdown (see _passes).
        audit = broker.audit() if broker is not None else None
        if audit is not None:
            unmeasured["autofix"] = max(0, audit.get("requests", 0) - len(audit.get("text_completions", [])))
            try:
                rows += receipts.build(
                    audit.get("text_completions", []), repository=target["repository"], run_id=run_id,
                    run_attempt=run_attempt, base_sha=target["base_sha"], head_sha=target["head_sha"],
                    workflow_sha=workflow_sha, role="autofix", data_sensitivity=config["data_sensitivity"],
                    correlation_id=f"{target['repository']}#{target['pull_request']}@{target['head_sha']}/autofix")
            except receipts.ReceiptError:
                unmeasured["autofix"] = audit.get("requests", 0)
            _checkpoint(checkpoint, rows)
    if output.get("status") != "completed":
        return {"outcome": "held", "reason": output.get("reason", "guest_held"), "detail": output.get("detail", "")}
    if output.get("outcome") != "candidate":
        return {"outcome": output.get("outcome", "no_safe_fix")}
    return write_packet(target, directory, output)


def prior_autofix_rounds(github, target):
    """Count [autofix] commits already on the PR: each published repair creates
    a new head and a new request, so the cap must come from native history."""
    try:
        commits = github.pages(f"/pulls/{int(target['pull_request'])}/commits")
    except (Incomplete, OSError, ValueError):
        return None
    if not isinstance(commits, list) or len(commits) >= 250:
        return None
    return sum(1 for c in commits if isinstance(c, dict)
               and str((c.get("commit") or {}).get("message") or "").startswith("[autofix]"))


def editor_admission(target):
    """Run the accepted autofix-custody.sh before any editor work: open PR at the
    exact head, no fork, no human/security holds, no session owner, no held or
    uncertain publication. Returns True or a hold reason."""
    script = HERE / "autofix-custody.sh"
    with tempfile.NamedTemporaryFile("r", suffix=".out") as out:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GH_TOKEN": os.environ.get("GH_TOKEN", ""),
               "REPOSITORY": target["repository"], "PR_NUMBER": str(int(target["pull_request"])),
               "EXPECTED_HEAD": target["head_sha"], "GITHUB_OUTPUT": out.name}
        try:
            subprocess.run(["bash", str(script)], env=env, capture_output=True, timeout=120, check=False)
        except (OSError, subprocess.SubprocessError):
            return "editor_admission_unavailable"
        result = out.read()
    return True if "skip=false" in result.split() else "editor_admission_held"


def write_packet(target, directory, output):
    """Exactly the accepted publisher's packet: receipt.json + implementation.bundle."""
    import base64
    candidate = output.get("candidate_sha", "")
    head_ref = target.get("head_ref", "")
    if (not re.fullmatch(r"[0-9a-f]{40}", candidate) or candidate == target["head_sha"]
            or not isinstance(head_ref, str) or not head_ref or head_ref in {"main", "master"}):
        return {"outcome": "failed", "reason": "packet_identity_invalid"}
    try:
        bundle = base64.b64decode(output.get("bundle_b64", ""), validate=True)
    except ValueError:
        return {"outcome": "failed", "reason": "packet_bundle_invalid"}
    packet = directory / "packet"
    packet.mkdir(mode=0o700)
    (packet / "implementation.bundle").write_bytes(bundle)
    heads = subprocess.run(["git", "bundle", "list-heads", str(packet / "implementation.bundle")],
                           capture_output=True, timeout=30, check=False,
                           env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    if heads.returncode or heads.stdout.decode().splitlines() != [candidate + " refs/heads/implementation"]:
        return {"outcome": "failed", "reason": "packet_bundle_refs_invalid"}
    (packet / "receipt.json").write_text(json.dumps({
        "repository": target["repository"], "pr_number": str(target["pull_request"]),
        "head_ref": head_ref, "base_sha": target["head_sha"], "candidate_sha": candidate}) + "\n")
    return {"outcome": "packet", "candidate_sha": candidate, "paths": output.get("paths", []),
            "summary": output.get("summary", ""), "packet_dir": str(packet)}


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


def _publish(github, target, result):
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
        out = state / "result"
        out.mkdir(exist_ok=True)
        # Name the retained artifact before any model work: a cancelled job
        # still uploads the per-pass receipt checkpoint.
        _output(artifact=f"codex-automation-result-{target['pull_request']}-{target['head_sha']}")
        # Never an empty retained directory, even if execution fails before a pass.
        _checkpoint(out / "inference-receipts.jsonl", [])
        credential = os.environ.pop("CODEX_AUTOMATION_DOPPLER_TOKEN", "")
        with tempfile.TemporaryDirectory(dir=state) as tmp:
            meta = state / "pr-meta.json"
            if hosted.enabled():
                try:
                    hosted.configure(policy(), Path(tmp) / "codex-home", credential)
                except profiles.ProfileError as exc:
                    result, rows = {"state": "blocked_credentials", "reason": str(exc)}, []
                    credential = None
            if credential is not None:
                result, rows = execute(github, target, Path(tmp), run_id=int(os.environ["GITHUB_RUN_ID"]),
                                   run_attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]),
                                   workflow_sha=os.environ["GITHUB_WORKFLOW_SHA"],
                                   pr_meta=json.loads(meta.read_text(encoding="utf-8")) if meta.is_file() else None,
                                   checkpoint=out / "inference-receipts.jsonl")
            packet = (result.get("autofix") or {}).get("packet_dir")
            if packet:
                # Retain the inert packet before the temporary editor workspace is removed.
                shutil.copytree(packet, state / "packet")
                result["autofix"]["packet_dir"] = "packet"
        if result["state"] not in STATES:
            result = {"state": "failed", "reason": "unknown_state"}
        (out / "result.json").write_text(json.dumps(result, sort_keys=True))
        (out / "inference-receipts.jsonl").write_text(receipts.dumps(rows))
        _output(state=result["state"])
        if (state / "packet").is_dir():
            _output(packet=f"codex-automation-packet-{target['pull_request']}-{target['head_sha']}",
                    pr_number=str(int(target["pull_request"])), expected_head=target["head_sha"],
                    head_ref=target["head_ref"])
        return 0
    if command == "release":
        # The run produced no result to publish; still close the advisory claim.
        custody.release(github, target, "ended without a result")
        return 0
    if command == "publish":
        result = json.loads((state / "result/result.json").read_text(encoding="utf-8"))
        try:
            return _publish(github, target, result)
        finally:
            # Advisory only: never leave the execution claim pending, which
            # would mark the pull request unstable and block native auto-merge.
            custody.release(github, target, str(result.get("state") or "finished"))
    raise SystemExit("usage: codex_automation_worker.py prepare|run|publish|release")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
