"""Guest entrypoint for the subscription automation assessment pipeline.

Runs only inside the credential-free, networkless container built from the
accepted source closure (`/opt/automation`). Candidate Git (`/input/repo`, bare,
read-only) is data: no candidate file is executed or imported. The single model
route is the host broker socket via `codex_automation_adapter`.

Two passes, one immutable context each (`/input/codex-automation.json`):

* ``assessment``: classifier, then every classifier-requested role. The stage
  runners enforce the controller roster (`selected_roles`) and write their own
  provenance-bound held markers for roles outside it; `held` reports them.
* ``judge``: reads candidates from `/input/candidates` (written by the host
  from the assessment pass) and runs the Judge's unchanged decision logic,
  capturing the publication it would make instead of performing it.

Stdout carries exactly one bounded JSON document; the host validates it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTEXT = Path("/input/codex-automation.json")
REPO = Path("/input/repo")
CANDIDATES_IN = Path("/input/candidates")
PR_META = Path("/input/pr-meta.json")
# Host bound: 1 KiB title + 64 KiB body of raw UTF-8; JSON escaping of quotes,
# backslashes and tab/newline/CR can at most double that.
MAX_PR_META_BYTES = 2 * (1024 + 64 * 1024) + 4096
STATE = Path("/state")
MAX_CANDIDATE_BYTES = 512 * 1024
MAX_REQUEST_CHARS = 180000  # codex_subscription_text.MAX_INPUT_CHARS
STAGE_SECONDS = 900
# Total guest budget; the host passes its own deadline minus a margin.
DEADLINE = time.monotonic() + int(os.environ.get("CODEX_AUTOMATION_GUEST_SECONDS", "1400"))


class GuestHold(RuntimeError):
    """A stage cannot produce trustworthy output; never fall back.

    ``detail`` is a bounded stderr tail for the retained result artifact only;
    the host never puts it into a PR comment.
    """

    def __init__(self, reason, detail=""):
        super().__init__(reason)
        self.detail = detail


def _context():
    spec = importlib.util.spec_from_file_location(
        "_automation_context", ROOT / "scripts/ci/codex_automation_context.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.read_context(CONTEXT, os.environ)


def _pr_meta():
    """Admitted PR title/body: untrusted data the stage runners wrap as such."""
    if not PR_META.exists():
        return {"title": "", "body": ""}
    raw = PR_META.read_bytes()
    try:
        meta = json.loads(raw.decode("utf-8")) if len(raw) <= MAX_PR_META_BYTES else None
    except ValueError:
        meta = None
    if (not isinstance(meta, dict) or set(meta) != {"title", "body"}
            or not all(isinstance(v, str) for v in meta.values())):
        raise GuestHold("pr_metadata_invalid")
    return meta


def _stage_env(value):
    meta = _pr_meta()
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(STATE),
        "CODEX_AUTOMATION_MODE": "required",
        "GITHUB_REPOSITORY": value["repository"],
        "PR_NUMBER": str(value["pull_request"]),
        "PR_BASE_SHA": value["base_sha"],
        "PR_HEAD_SHA": value["head_sha"],
        "GITHUB_RUN_ID": str(value["controller_run_id"]),
        "GITHUB_RUN_ATTEMPT": str(value["controller_run_attempt"]),
        "GITHUB_WORKSPACE": str(REPO),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PR_TITLE": meta["title"],
        "PR_BODY": meta["body"],
    }
    return env


def _workspace():
    """Writable cwd holding trusted definitions copied from the image."""
    work = STATE / "work"
    if work.exists():
        shutil.rmtree(work)
    (work / ".claude").mkdir(parents=True)
    for name in ("agents", "skills"):
        shutil.copytree(ROOT / ".claude" / name, work / ".claude" / name)
    return work


def _diff(value, work):
    path = work / "pr.diff"
    result = subprocess.run(
        ["git", "--no-pager", "--no-replace-objects", "-C", str(REPO), "-c", "core.fsmonitor=false",
         "diff", "--no-ext-diff", "--no-textconv", value["base_sha"] + "..." + value["head_sha"]],
        capture_output=True, timeout=120, check=False,
        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
             "GIT_TERMINAL_PROMPT": "0"})
    if result.returncode:
        raise GuestHold("candidate_diff_unavailable")
    path.write_bytes(result.stdout)
    return path


WORKFLOW_PATH = re.compile(r"(?:^|/)\.github/workflows/[^/]+\.ya?ml$")
MAX_WORKFLOW_BYTES = 256 * 1024
MAX_WORKFLOW_TOTAL = 2 * 1024 * 1024


def _workflow_heads(value, work):
    """Copy changed workflow files at the admitted head into the workspace.

    The classifier's deterministic deployment floor reads complete workflow
    files relative to its cwd (including a rename target, so a workflow moved
    out of .github/workflows/ is still seen); without them it sees only changed
    lines and can drop the required systems-consistency review. These bytes are
    data for that reader only: never executed, bounded, confined to the workspace.
    """
    result = subprocess.run(
        ["git", "--no-pager", "--no-replace-objects", "-C", str(REPO), "-c", "core.quotePath=false",
         "diff", "--name-status", "-z", "-M", "--diff-filter=AMR",
         value["base_sha"] + "..." + value["head_sha"]],
        capture_output=True, timeout=60, check=False,
        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
             "GIT_TERMINAL_PROMPT": "0"})
    if result.returncode:
        raise GuestHold("candidate_diff_unavailable")
    fields = [f.decode("utf-8", "replace") for f in result.stdout.split(b"\0")]
    targets, index = [], 0
    while index < len(fields) and fields[index]:
        status = fields[index]
        if status.startswith("R"):
            old, new = fields[index + 1], fields[index + 2]
            index += 3
            if WORKFLOW_PATH.search(old) or WORKFLOW_PATH.search(new):
                targets.append(new)
        else:
            path = fields[index + 1]
            index += 2
            if WORKFLOW_PATH.search(path):
                targets.append(path)
    total = 0
    for path in targets:
        parts = Path(path).parts
        if (not parts or Path(path).is_absolute() or ".." in parts or parts[0] in (".claude", ".ai")
                or "\ufffd" in path):
            raise GuestHold("candidate_workflow_path_invalid")
        shown = subprocess.run(
            ["git", "--no-pager", "-C", str(REPO), "show", f"{value['head_sha']}:{path}"],
            capture_output=True, timeout=30, check=False,
            env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        if shown.returncode:
            # Never an empty stand-in: the floor would fail open for this file.
            raise GuestHold("candidate_workflow_unreadable")
        content = shown.stdout
        if len(content) > MAX_WORKFLOW_BYTES or total + len(content) > MAX_WORKFLOW_TOTAL:
            raise GuestHold("candidate_workflow_over_budget")
        total += len(content)
        target = work / path
        try:
            if target.exists():
                raise OSError("workspace path collision")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        except OSError:
            raise GuestHold("candidate_workflow_path_invalid") from None


def _remaining():
    """Stages share one total budget bounded by the host's guest deadline."""
    remaining = DEADLINE - time.monotonic()
    if remaining < 30:
        raise GuestHold("guest_time_budget_exhausted")
    return min(STAGE_SECONDS, remaining - 15)


def _run(script, args, value, work):
    result = subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts" / script), *args],
        cwd=work, env=_stage_env(value), capture_output=True, timeout=_remaining(), check=False)
    if result.returncode:
        tail = result.stderr.decode("utf-8", "replace")[-600:]
        raise GuestHold(f"stage_failed:{script}", tail)


def _candidates(directory):
    rows = {}
    total = 0
    for path in sorted(Path(directory).glob("*.json")):
        raw = path.read_bytes()
        total += len(raw)
        if total > MAX_CANDIDATE_BYTES:
            raise GuestHold("candidate_output_over_budget")
        rows[path.name] = json.loads(raw)
    return rows


def assessment(value):
    work = _workspace()
    diff = _diff(value, work)
    # Subscription classification requires the complete diff. A diff alone
    # over the request limit cannot fit once the prompt and metadata are added.
    if (diff.stat().st_size > MAX_REQUEST_CHARS
            and len(diff.read_text(encoding="utf-8", errors="replace")) > MAX_REQUEST_CHARS):
        raise GuestHold("subscription_text_input_exhausted")
    _workflow_heads(value, work)
    manifest = work / "classifier-output.json"
    candidates = work / ".ai/candidates"
    meta = _pr_meta()
    _run("run-pr-classifier.py", ["--diff", str(diff), "--output", str(manifest),
                                  # `=` form: a title/body starting with "-" stays a value.
                                  "--title=" + meta["title"], "--body=" + meta["body"]], value, work)
    plan = json.loads(manifest.read_text(encoding="utf-8"))
    roster = set(value["selected_roles"])
    requested = {"skills": list(plan.get("invoke_skills", [])), "agents": list(plan.get("invoke_agents", []))}
    if any(not isinstance(name, str) or "," in name for names in requested.values() for name in names):
        raise GuestHold("classifier_manifest_invalid")
    # The stage runners enforce the roster themselves and write provenance-bound
    # held markers for unselected roles; never forge candidate files here.
    held = sorted({n for names in requested.values() for n in names} - roster)
    run = requested
    candidates.mkdir(parents=True, exist_ok=True)
    if run["skills"]:
        _run("run-specialists.py", ["--skills", ",".join(run["skills"]), "--diff", str(diff),
                                    "--candidates-dir", str(candidates)], value, work)
    if run["agents"]:
        _run("run-broad-agents.py", ["--agents", ",".join(run["agents"]), "--diff", str(diff),
                                     "--candidates-dir", str(candidates)], value, work)
    return {"pass": "assessment", "depth": plan.get("depth"), "ran": run, "held": held,
            "candidates": _candidates(candidates)}


def _load_judge():
    spec = importlib.util.spec_from_file_location("_guest_judge", ROOT / "scripts/run-judge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def judge(value, module=None):
    work = _workspace()
    candidates = work / ".ai/candidates"
    shutil.copytree(CANDIDATES_IN, candidates)
    for key, item in _stage_env(value).items():
        os.environ[key] = item
    os.chdir(work)
    module = module or _load_judge()
    captured = []

    class CapturingJudge(module.Judge):
        """Unchanged decision logic; publication is captured for the host."""

        def post_review_to_github(self, surviving, review_action):
            captured.append({"action": "review", "event": review_action, "surviving": surviving})
            return True

        def _post_advisory_warning(self, detail=""):
            captured.append({"action": "advisory", "detail": detail})
            return True

        def _dismiss_stale_judge_reviews(self, states=("CHANGES_REQUESTED",)):
            captured.append({"action": "dismiss", "states": list(states)})

        def _post_no_findings_comment(self):
            captured.append({"action": "no_findings"})

    instance = CapturingJudge(candidates_dir=str(candidates), pr_number=value["pull_request"],
                              sha=value["head_sha"])
    instance.repo = value["repository"]
    instance.token = ""
    code = instance.run()
    return {"pass": "judge", "returncode": code, "publication": captured}


AUTOFIX_SYSTEM = (
    "You repair a pull request. Return exactly one JSON object with keys \"summary\" "
    "(one line, at most 100 characters, no trailing period) and \"patch\" (a unified Git diff "
    "using 'diff --git a/PATH b/PATH' headers that only modifies existing files or adds new "
    "regular files). Fix only the listed findings. Never touch CI workflows, agent or "
    "instruction files, or delete or rename files. If you cannot fix them safely, return "
    "{\"summary\": \"no safe fix\", \"patch\": \"\"}."
)
MAX_CONTEXT_BYTES = 120 * 1024
MAX_BUNDLE_BYTES = 1024 * 1024


def _load(name):
    spec = importlib.util.spec_from_file_location("_guest_" + name, ROOT / "scripts/ci" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _adapter():
    """The accepted guest loader enforces required mode, no credentials and an
    immutable context before returning the adapter (as the stage runners do)."""
    path = ROOT / "scripts/lib/subscription_guest.py"
    spec = importlib.util.spec_from_file_location("_guest_subscription_loader", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.runtime()


def _show(value, path):
    result = subprocess.run(
        ["git", "--no-pager", "-C", str(REPO), "show", f"{value['head_sha']}:{path}"],
        capture_output=True, timeout=30, check=False,
        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    return result.stdout if result.returncode == 0 else b""


def autofix(value, adapter=None, patcher=None):
    """One tool-free proposal for the Judge's surviving findings; never retried."""
    os.environ.update(_stage_env(value))
    adapter = adapter or _adapter()
    patcher = patcher or _load("codex_automation_patch")
    findings = json.loads((CANDIDATES_IN / "autofix-findings.json").read_text(encoding="utf-8"))
    if not isinstance(findings, list) or not findings or len(findings) > 20:
        raise GuestHold("autofix_findings_invalid")
    files, used = {}, 0
    for finding in findings:
        path = finding.get("file") if isinstance(finding, dict) else None
        if not isinstance(path, str) or path in files:
            continue
        content = _show(value, path)
        if used + len(content) > MAX_CONTEXT_BYTES:
            raise GuestHold("autofix_context_over_budget")
        used += len(content)
        files[path] = content.decode("utf-8", "replace")
    # Raw Unicode: escaping CJK/emoji would multiply the size past the transport budget.
    user = json.dumps({"findings": findings, "files_at_head": files}, sort_keys=True, ensure_ascii=False)
    if len(AUTOFIX_SYSTEM) + len(user) > MAX_REQUEST_CHARS:
        raise GuestHold("autofix_context_over_budget")
    text = adapter.complete(AUTOFIX_SYSTEM, user, role="autofix",
                            data_sensitivity=value["data_sensitivity"], max_tokens=8000)
    try:
        raw = json.loads(text)
    except ValueError:
        raw = None
    if isinstance(raw, dict) and set(raw) == {"summary", "patch"} and raw["patch"] == "":
        return {"pass": "autofix", "outcome": "no_safe_fix"}
    proposal = patcher.parse_proposal(text)
    work = STATE / "autofix"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    candidate = patcher.build_candidate(REPO, work, value["head_sha"], proposal)
    bundle = candidate["bundle"].read_bytes()
    if len(bundle) > MAX_BUNDLE_BYTES:
        raise GuestHold("autofix_bundle_over_budget")
    import base64
    return {"pass": "autofix", "outcome": "candidate", "candidate_sha": candidate["candidate_sha"],
            "paths": candidate["paths"], "summary": proposal["summary"],
            "bundle_b64": base64.b64encode(bundle).decode()}


def main():
    try:
        value = _context()
        result = {"assessment": assessment, "judge": judge, "autofix": autofix}.get(value["role"])
        if result is None:
            raise GuestHold("unsupported_guest_role")
        print(json.dumps({"status": "completed", **result(value)}, sort_keys=True))
        return 0
    except (GuestHold, ValueError, OSError, RuntimeError, ImportError, subprocess.SubprocessError,
            json.JSONDecodeError) as exc:
        coded = isinstance(exc, (GuestHold, RuntimeError)) or type(exc).__name__ == "PatchRefused"
        reason = str(exc) if coded and str(exc) else type(exc).__name__
        held = {"status": "held", "reason": reason[:200]}
        if getattr(exc, "detail", ""):
            held["detail"] = exc.detail[-600:]
        print(json.dumps(held))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
