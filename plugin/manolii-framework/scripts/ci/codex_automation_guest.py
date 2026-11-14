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
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTEXT = Path("/input/codex-automation.json")
REPO = Path("/input/repo")
CANDIDATES_IN = Path("/input/candidates")
STATE = Path("/state")
MAX_CANDIDATE_BYTES = 512 * 1024
STAGE_SECONDS = 900


class GuestHold(RuntimeError):
    """A stage cannot produce trustworthy output; never fall back."""


def _context():
    spec = importlib.util.spec_from_file_location(
        "_automation_context", ROOT / "scripts/ci/codex_automation_context.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.read_context(CONTEXT, os.environ)


def _stage_env(value):
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


def _run(script, args, value, work):
    result = subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts" / script), *args],
        cwd=work, env=_stage_env(value), capture_output=True, timeout=STAGE_SECONDS, check=False)
    if result.returncode:
        raise GuestHold(f"stage_failed:{script}")


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
    manifest = work / "classifier-output.json"
    candidates = work / ".ai/candidates"
    _run("run-pr-classifier.py", ["--diff", str(diff), "--output", str(manifest)], value, work)
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

    instance = CapturingJudge(candidates_dir=str(candidates), pr_number=value["pull_request"],
                              sha=value["head_sha"])
    instance.repo = value["repository"]
    instance.token = ""
    code = instance.run()
    return {"pass": "judge", "returncode": code, "publication": captured}


def main():
    try:
        value = _context()
        result = {"assessment": assessment, "judge": judge}.get(value["role"])
        if result is None:
            raise GuestHold("unsupported_guest_role")
        print(json.dumps({"status": "completed", **result(value)}, sort_keys=True))
        return 0
    except (GuestHold, ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        reason = str(exc) if isinstance(exc, GuestHold) else type(exc).__name__
        print(json.dumps({"status": "held", "reason": reason[:200]}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
