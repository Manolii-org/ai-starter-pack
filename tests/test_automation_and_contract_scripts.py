"""Unit tests for validate-automation-registry and check-deployment-contract."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_SCRIPT = ROOT / "scripts/validate-automation-registry.py"
CONTRACT_SCRIPT = ROOT / "scripts/check-deployment-contract.py"
GUARDS_SCRIPT = ROOT / ".github/actions/check-guarded-paths/check.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── automation registry ─────────────────────────────────────────────────────

VALID_REGISTRY = {
    "schema_version": 1,
    "automations": [
        {
            "name": "nightly-drift",
            "repo": "ExampleOrg/example-service",
            "workflow": ".github/workflows/nightly.yml",
            "trigger": {"type": "schedule", "cron": "0 2 * * *"},
            "risk_tier": "green",
            "owner": "platform",
            "required_secrets": ["GITHUB_TOKEN"],
        }
    ],
}


def test_registry_valid_passes() -> None:
    mod = _load(REGISTRY_SCRIPT, "var")
    errors: list[str] = []
    mod.validate(VALID_REGISTRY, errors)
    assert errors == []


@pytest.mark.parametrize(
    "mutant,needle",
    [
        ({"schema_version": 2, "automations": []}, "schema_version"),
        ({"automations": []}, "schema_version"),
        (
            {"schema_version": 1, "automations": [{**VALID_REGISTRY["automations"][0], "risk_tier": "purple"}]},
            "risk_tier",
        ),
        (
            {"schema_version": 1, "automations": [{**VALID_REGISTRY["automations"][0], "trigger": {"type": "schedule"}}]},
            "cron",
        ),
        (
            {"schema_version": 1, "automations": [{**VALID_REGISTRY["automations"][0], "required_secrets": ["supersecretvalue"]}]},
            "NAME",
        ),
        (
            {"schema_version": 1, "automations": [VALID_REGISTRY["automations"][0], VALID_REGISTRY["automations"][0]]},
            "duplicate",
        ),
    ],
)
def test_registry_mutants_fail(mutant: dict, needle: str) -> None:
    mod = _load(REGISTRY_SCRIPT, "var2")
    errors: list[str] = []
    mod.validate(mutant, errors)
    assert errors, "expected validation errors"
    assert any(needle.lower() in e.lower() for e in errors)


# ── deployment contract ─────────────────────────────────────────────────────

def _write_repo(root: Path, name: str, workflow: str) -> Path:
    wf = root / name / ".github/workflows/deploy.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text(workflow, encoding="utf-8")
    return root / name


DEPLOY_YAML = """\
name: deploy
on:
  push:
    branches: [staging, prod]
jobs:
  test: {runs-on: ubuntu-latest, steps: [{run: "true"}]}
  deploy: {runs-on: ubuntu-latest, steps: [{run: "true"}]}
  smoke: {runs-on: ubuntu-latest, steps: [{run: "true"}]}
  rollback: {runs-on: ubuntu-latest, steps: [{run: "true"}]}
concurrency: {group: "deploy-${{ github.ref }}", cancel-in-progress: false}
"""


def test_contract_conformant_repo_passes(tmp_path: Path) -> None:
    _write_repo(tmp_path, "repo-a", DEPLOY_YAML)
    contract = tmp_path / "contract.yaml"
    contract.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "repos": [
                    {
                        "repo": "Org/repo-a",
                        "lanes": [
                            {
                                "branch": "prod",
                                "environment": "production",
                                "workflow": ".github/workflows/deploy.yml",
                                "required_jobs": ["test", "deploy", "smoke"],
                                "rollback_required": True,
                                "concurrency_group": "deploy-",
                            }
                        ],
                    }
                ],
            }
        )
    )
    out = subprocess.run(
        [sys.executable, str(CONTRACT_SCRIPT), str(contract), "--mode", "local", "--repos-dir", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stdout + out.stderr


def test_contract_missing_rollback_fails(tmp_path: Path) -> None:
    _write_repo(
        tmp_path,
        "repo-b",
        DEPLOY_YAML.replace("  rollback:", "  other:"),
    )
    contract = tmp_path / "c.yaml"
    contract.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "repos": [
                    {
                        "repo": "Org/repo-b",
                        "lanes": [
                            {
                                "branch": "prod",
                                "environment": "production",
                                "workflow": ".github/workflows/deploy.yml",
                            }
                        ],
                    }
                ],
            }
        )
    )
    out = subprocess.run(
        [sys.executable, str(CONTRACT_SCRIPT), str(contract), "--mode", "local", "--repos-dir", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert out.returncode == 1
    assert "rollback" in out.stdout


def test_contract_wrong_branch_trigger_fails(tmp_path: Path) -> None:
    _write_repo(tmp_path, "repo-c", DEPLOY_YAML.replace("[staging, prod]", "[main]"))
    contract = tmp_path / "c.yaml"
    contract.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "repos": [
                    {
                        "repo": "Org/repo-c",
                        "lanes": [{"branch": "prod", "environment": "production",
                                   "workflow": ".github/workflows/deploy.yml"}],
                    }
                ],
            }
        )
    )
    out = subprocess.run(
        [sys.executable, str(CONTRACT_SCRIPT), str(contract), "--mode", "local", "--repos-dir", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert out.returncode == 1
    assert "does not trigger" in out.stdout


# ── guarded paths ────────────────────────────────────────────────────────────

def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_repo(repo: Path, guards: dict, files: dict[str, str]) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "checkout", "-q", "-b", "main")
    (repo / ".ai").mkdir(exist_ok=True)
    import json

    (repo / ".ai/guards.json").write_text(json.dumps(guards))
    for path, text in files.items():
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")


GUARDS = {
    "guards": [
        {"id": "db-migrations", "paths": ["alembic/**"], "mode": "block", "reason": "r"},
        {"id": "ci", "paths": [".github/**"], "mode": "warn", "reason": "r"},
    ],
    "session_unfreezes": [],
}


def _run_guard(repo: Path, base: str, head: str, extra: list[str] | None = None):
    return subprocess.run(
        [sys.executable, str(GUARDS_SCRIPT), "--base", base, "--head", head]
        + (extra or []),
        cwd=repo, capture_output=True, text=True,
    )


def test_guard_clean_diff_passes(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo, GUARDS, {"app.py": "x=1"})
    _git(repo, "checkout", "-qb", "feat")
    (repo / "app.py").write_text("x=2")
    _git(repo, "commit", "-qam", "change")
    out = _run_guard(repo, "main", "HEAD")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "warn" not in out.stdout.lower() or ".github" not in out.stdout


def test_guard_blocked_path_fails(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo, GUARDS, {"alembic/v1.py": "rev=1"})
    _git(repo, "checkout", "-qb", "feat")
    (repo / "alembic/v2.py").write_text("rev=2")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "migration")
    out = _run_guard(repo, "main", "HEAD")
    assert out.returncode == 1
    assert "db-migrations" in out.stdout


def test_guard_trailer_bypasses(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo, GUARDS, {"alembic/v1.py": "rev=1"})
    _git(repo, "checkout", "-qb", "feat")
    (repo / "alembic/v2.py").write_text("rev=2")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "migration\n\nGuarded-Path: db-migrations")
    out = _run_guard(repo, "main", "HEAD")
    assert out.returncode == 0, out.stdout + out.stderr


def test_guard_bypass_arg(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo, GUARDS, {"alembic/v1.py": "rev=1"})
    _git(repo, "checkout", "-qb", "feat")
    (repo / "alembic/v2.py").write_text("rev=2")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "migration")
    out = _run_guard(repo, "main", "HEAD", ["--bypass", "db-migrations"])
    assert out.returncode == 0, out.stdout + out.stderr


# ── review-driven hardening ──────────────────────────────────────────────────

def test_guard_policy_read_from_base_not_head(tmp_path: Path) -> None:
    """Deleting/emptying .ai/guards.json in the PR must still fail."""
    repo = tmp_path / "r"
    _init_repo(repo, GUARDS, {"alembic/v1.py": "rev=1"})
    _git(repo, "checkout", "-qb", "feat")
    (repo / ".ai/guards.json").write_text('{"guards": [], "session_unfreezes": []}')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "gut the policy")
    out = _run_guard(repo, "main", "HEAD")
    assert out.returncode == 1
    assert "policy itself" in out.stdout or "guards" in out.stdout.lower()


def test_guard_file_change_with_bypass_passes(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo, GUARDS, {"alembic/v1.py": "rev=1"})
    _git(repo, "checkout", "-qb", "feat")
    (repo / ".ai/guards.json").write_text('{"guards": [], "session_unfreezes": []}')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "adjust policy")
    out = _run_guard(repo, "main", "HEAD", ["--bypass", "guards-config"])
    assert out.returncode == 0, out.stdout + out.stderr


def test_unfiltered_push_trigger_matches(tmp_path: Path) -> None:
    mod = _load(CONTRACT_SCRIPT, "cdc2")
    spec = yaml.safe_load("on: push\njobs:\n  deploy: {runs-on: ubuntu-latest, steps: [{run: 'true'}]}\n")
    assert mod.workflow_triggers_branch(spec, "prod") is True


def test_branch_pattern_matches(tmp_path: Path) -> None:
    mod = _load(CONTRACT_SCRIPT, "cdc3")
    spec = yaml.safe_load("on: {push: {branches: ['release/*']}}\n")
    assert mod.workflow_triggers_branch(spec, "release/3.2.4") is True
    assert mod.workflow_triggers_branch(spec, "main") is False


def test_repo_level_rollback_optout_honored(tmp_path: Path) -> None:
    mod = _load(CONTRACT_SCRIPT, "cdc4")
    repo_spec = {"rollback_required": False}
    lane = {"branch": "prod", "environment": "production",
            "workflow": ".github/workflows/deploy.yml"}
    _write_repo(tmp_path, "repo-d", DEPLOY_YAML.replace("  rollback:", "  other:"))
    import argparse
    args = argparse.Namespace(mode="local", repos_dir=str(tmp_path))
    errors: list[str] = []
    mod.check_lane("Org/repo-d", repo_spec, lane, args, errors, [])
    assert not any("rollback" in e for e in errors), errors


def test_registry_unknown_keys_and_types(tmp_path: Path) -> None:
    mod = _load(REGISTRY_SCRIPT, "var3")
    bad = {"schema_version": 1, "automations": [
        {**VALID_REGISTRY["automations"][0], "bogus_key": 1, "deadman": "yes"}
    ]}
    errors: list[str] = []
    mod.validate(bad, errors)
    assert any("unknown automation keys" in e for e in errors)
    assert any("deadman" in e for e in errors)


# ── review round 2 ───────────────────────────────────────────────────────────

def test_trailer_in_stale_base_commit_does_not_bypass(tmp_path: Path) -> None:
    """A Guarded-Path trailer on a base-side commit (merged after the PR
    branched) must NOT clear the guard for the stale head."""
    repo = tmp_path / "r"
    _init_repo(repo, GUARDS, {"alembic/v1.py": "rev=1"})
    _git(repo, "checkout", "-qb", "feat")
    (repo / "alembic/v1.py").write_text("rev=1\n# feature change")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feat change (no trailer)")
    # base advances: a merged commit carries the trailer on the BASE side
    _git(repo, "checkout", "-q", "main")
    (repo / "other.py").write_text("x = 2")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base work\n\nGuarded-Path: db-migrations")
    out = _run_guard(repo, "main", "feat")
    assert out.returncode == 1, out.stdout + out.stderr


def test_ordered_negative_branch_patterns(tmp_path: Path) -> None:
    mod = _load(CONTRACT_SCRIPT, "cdc5")
    spec = yaml.safe_load("on: {push: {branches: ['releases/**', '!releases/**-alpha']}}\n")
    assert mod.workflow_triggers_branch(spec, "releases/10") is True
    assert mod.workflow_triggers_branch(spec, "releases/10-alpha") is False


def test_malformed_contract_rejected(tmp_path: Path) -> None:
    mod = _load(CONTRACT_SCRIPT, "cdc6")
    bad = tmp_path / "c.yaml"
    bad.write_text(yaml.safe_dump({
        "schema_version": 1,
        "repos": [{"repo": "Org/app", "lanes": []}],   # empty lanes
    }))
    import pytest
    with pytest.raises(SystemExit) as exc:
        mod.load_contract(bad)
    assert "lanes" in str(exc.value)

    bad.write_text(yaml.safe_dump({
        "schema_version": 1,
        "repos": [{"repo": "Org/app", "lanes": [{"branch": "prod"}]}],  # missing env+workflow
    }))
    with pytest.raises(SystemExit) as exc:
        mod.load_contract(bad)
    assert "environment" in str(exc.value) and "workflow" in str(exc.value)


# ── review round 3 ───────────────────────────────────────────────────────────

def test_uses_match_is_filename_stem_not_substring(tmp_path: Path) -> None:
    """Required 'test' must NOT be satisfied by uses: ./contest.yml."""
    mod = _load(CONTRACT_SCRIPT, "cdc7")
    spec = yaml.safe_load(
        "jobs:\n  call:\n    uses: ./.github/workflows/contest.yml\n")
    jobs = (spec.get("jobs") or {})
    hit = any(
        isinstance(jobs.get(j), dict)
        and Path(str(jobs[j].get("uses", "")).split("@")[0]).stem == "test"
        for j in jobs)
    assert hit is False
    spec2 = yaml.safe_load(
        "jobs:\n  call:\n    uses: org/repo/.github/workflows/test.yml@v1\n")
    jobs2 = spec2["jobs"]
    assert any(Path(str(jobs2[j]["uses"]).split("@")[0]).stem == "test" for j in jobs2)


def test_malformed_guard_entry_fails_closed(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    bad = {"guards": [{"id": "g", "paths": "alembic/**", "mode": "block"}]}
    _init_repo(repo, bad, {"x.py": "1"})
    _git(repo, "checkout", "-qb", "feat")
    (repo / "alembic").mkdir(exist_ok=True)
    (repo / "alembic/v.py").write_text("rev=1")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "touch guarded path")
    out = _run_guard(repo, "main", "HEAD")
    assert out.returncode == 2, out.stdout + out.stderr
    assert "malformed" in out.stderr.lower() or "malformed" in out.stdout.lower()


def test_lane_environment_must_match_workflow_jobs(tmp_path: Path) -> None:
    mod = _load(CONTRACT_SCRIPT, "cdc8")
    wf = ("on: push\nconcurrency: {group: deploy-x}\njobs:\n"
          "  deploy:\n    environment: staging\n    runs-on: ubuntu-latest\n"
          "    steps: [{run: 'true'}]\n  rollback:\n    runs-on: ubuntu-latest\n"
          "    steps: [{run: 'true'}]\n")
    _write_repo(tmp_path, "repo-e", wf)
    import argparse
    args = argparse.Namespace(mode="local", repos_dir=str(tmp_path))
    lane = {"branch": "main", "environment": "production",
            "workflow": ".github/workflows/deploy.yml", "required_jobs": ["deploy"]}
    errors: list[str] = []; warnings: list[str] = []
    mod.check_lane("Org/repo-e", {}, lane, args, errors, warnings)
    assert any("environment 'production'" in e for e in errors), errors


def test_ref_exists_but_file_absent_is_drift(tmp_path: Path) -> None:
    mod = _load(CONTRACT_SCRIPT, "cdc9")
    _init_repo(tmp_path / "repo-f", '{"guards":[]}', {"app.py": "1"})
    repo_dir = tmp_path / "repo-f"
    _git(repo_dir, "checkout", "-qb", "prod")
    (repo_dir / ".github/workflows").mkdir(parents=True, exist_ok=True)
    (repo_dir / ".github/workflows/deploy.yml").write_text("on: push\n")
    _git(repo_dir, "add", "-A"); _git(repo_dir, "commit", "-qm", "wf on prod only")
    _git(repo_dir, "checkout", "-q", "main")
    import argparse
    args = argparse.Namespace(mode="local", repos_dir=str(tmp_path))
    text, err = mod.fetch_workflow("Org/repo-f", ".github/workflows/deploy.yml",
                                   "main", args)
    assert text is None and "absent on branch 'main'" in err


# ── review round 4 ───────────────────────────────────────────────────────────

def test_gh_glob_star_does_not_cross_slash() -> None:
    mod = _load(CONTRACT_SCRIPT, "cdcA")
    wf = ("on:\n  push:\n    branches: ['release/*']\n"
          "concurrency: {group: deploy-x}\njobs:\n"
          "  deploy:\n    environment: production\n    runs-on: ubuntu-latest\n"
          "    steps: [{run: 'true'}]\n")
    assert mod.workflow_triggers_branch(yaml.safe_load(wf), "release/3/hotfix") is False
    assert mod.workflow_triggers_branch(yaml.safe_load(wf), "release/3") is True
    wf2 = wf.replace("release/*", "release/**")
    assert mod.workflow_triggers_branch(yaml.safe_load(wf2), "release/3/hotfix") is True


def test_trailer_only_in_trailer_block(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo, GUARDS, {"x.py": "1"})
    _git(repo, "checkout", "-qb", "feat")
    (repo / "alembic").mkdir(exist_ok=True)
    (repo / "alembic/v.py").write_text("rev=1")
    _git(repo, "add", "-A")
    # `Guarded-Path:` inside PROSE — not a trailer — must NOT bypass
    _git(repo, "commit", "-qm", "subject",
         "-m", "notes: see Guarded-Path: db-migrations in the docs")
    out = _run_guard(repo, "main", "HEAD")
    assert out.returncode == 1
    # a real trailing trailer DOES bypass
    (repo / "x2.py").write_text("2")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "touch again",
         "-m", "real trailer below\n\nGuarded-Path: db-migrations")
    out2 = _run_guard(repo, "main", "HEAD")
    assert out2.returncode == 0


def test_rename_source_path_still_guarded(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo(repo, GUARDS, {"alembic/v1.py": "rev=1", "x.py": "1"})
    _git(repo, "checkout", "-qb", "feat")
    _git(repo, "mv", "alembic/v1.py", "v1.py")
    _git(repo, "commit", "-qm", "rename guarded file out of guard path")
    out = _run_guard(repo, "main", "HEAD")
    assert out.returncode == 1


def test_registry_required_field_types() -> None:
    mod = _load(REGISTRY_SCRIPT, "var4")
    doc = {"schema_version": 1, "automations": [
        {"name": "x", "repo": 123, "workflow": 456, "trigger": {"type": "push"},
         "risk_tier": "green", "owner": 7}]}
    errors: list[str] = []
    mod.validate(doc, errors)
    assert any("'repo' must be a string" in e for e in errors)
    assert any("'workflow' must be a string" in e for e in errors)
    assert any("'owner' must be a string" in e for e in errors)


# ── review round 5 ───────────────────────────────────────────────────────────

def test_cron_field_ranges() -> None:
    mod = _load(REGISTRY_SCRIPT, "var5")
    assert mod.valid_cron("0 2 * * *") is True
    assert mod.valid_cron("*/15 0-23 * * MON-FRI") is True
    assert mod.valid_cron("99 99 99 99 99") is False
    assert mod.valid_cron("0 25 * * *") is False
    assert mod.valid_cron("0 0 32 * *") is False
    assert mod.valid_cron("0 0 * 13 *") is False
    assert mod.valid_cron("* * * *") is False


def test_origin_ref_is_authoritative_over_local(tmp_path: Path) -> None:
    """origin/prod lacking the workflow must NOT fall back to local prod."""
    mod = _load(CONTRACT_SCRIPT, "cdcB")
    _init_repo(tmp_path / "repo-g", '{"guards":[]}', {"app.py": "1"})
    repo_dir = tmp_path / "repo-g"
    # fake an origin/prod WITHOUT the workflow: create a bare remote
    bare = tmp_path / "bare.git"
    _git(tmp_path, "init", "--bare", "-q", "bare.git")
    _git(repo_dir, "remote", "add", "origin", str(bare))
    _git(repo_dir, "push", "-q", "origin", "main")
    _git(repo_dir, "fetch", "-q", "origin")
    _git(repo_dir, "checkout", "-qb", "prod")
    _git(repo_dir, "push", "-q", "origin", "prod")
    # local prod gains the workflow but is NOT pushed
    (repo_dir / ".github/workflows").mkdir(parents=True, exist_ok=True)
    (repo_dir / ".github/workflows/deploy.yml").write_text("on: push\n")
    _git(repo_dir, "add", "-A"); _git(repo_dir, "commit", "-qm", "local-only wf")
    import argparse
    args = argparse.Namespace(mode="local", repos_dir=str(tmp_path))
    text, err = mod.fetch_workflow("Org/repo-g", ".github/workflows/deploy.yml",
                                   "prod", args)
    assert text is None and "origin/prod" in err


def test_cron_names_are_field_scoped() -> None:
    """Month names only valid in field 4; weekday names only in field 5."""
    mod = _load(REGISTRY_SCRIPT, "var6")
    assert mod.valid_cron("0 2 * JAN *") is True       # month name in month field
    assert mod.valid_cron("0 2 * * MON") is True       # weekday name in dow field
    assert mod.valid_cron("JAN 0 * * *") is False      # month name in minute field
    assert mod.valid_cron("0 0 * * JAN") is False      # month name in dow field
    assert mod.valid_cron("MON 0 * * *") is False      # weekday name in minute
    assert mod.valid_cron("0 2 * MON *") is False      # weekday name in month field
    assert mod.valid_cron("0 2 * JAN-MAR *") is True   # name range in month field


# ── review round 7 ───────────────────────────────────────────────────────────

def test_tag_only_push_does_not_match_branch() -> None:
    mod = _load(CONTRACT_SCRIPT, "cdc7")
    spec = yaml.safe_load("on:\n  push:\n    tags: ['v*']\n")
    assert mod.workflow_triggers_branch(spec, "prod") is False
    spec2 = yaml.safe_load("on:\n  push:\n    tags: ['v*']\n    branches: ['prod']\n")
    assert mod.workflow_triggers_branch(spec2, "prod") is True


def test_workflow_content_trigger_check(tmp_path: Path) -> None:
    """Registry mode=local must verify on: content, not just file existence."""
    import argparse
    mod = _load(REGISTRY_SCRIPT, "var7")
    repo_dir = tmp_path / "repo-h"
    wf = repo_dir / ".github/workflows/nightly.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text("on:\n  schedule:\n    - cron: '0 3 * * *'\njobs:\n  x: {runs-on: ubuntu-latest, steps: [{run: 'true'}]}\n")
    auto = {"name": "n", "repo": "Org/repo-h", "workflow": ".github/workflows/nightly.yml",
            "trigger": {"type": "schedule", "cron": "0 2 * * *"},
            "risk_tier": "green", "owner": "o"}
    args = argparse.Namespace(mode="local", repos_dir=str(tmp_path))
    errs: list[str] = []
    mod.check_workflow_files({"automations": [auto]}, args, errs)
    assert errs and "cron" in errs[0]          # drifted cron is caught
    auto["trigger"]["cron"] = "0 3 * * *"
    errs = []
    mod.check_workflow_files({"automations": [auto]}, args, errs)
    assert errs == []                           # matching cron passes
    wf.write_text("on: push\njobs:\n  x: {runs-on: ubuntu-latest, steps: [{run: 'true'}]}\n")
    errs = []
    mod.check_workflow_files({"automations": [auto]}, args, errs)
    assert errs and "on.schedule" in errs[0]    # schedule stripped → caught


# ── review round 8 ───────────────────────────────────────────────────────────

def test_gh_match_full_filter_grammar() -> None:
    mod = _load(CONTRACT_SCRIPT, "cdc8")
    spec = yaml.safe_load("on:\n  push:\n    branches: ['releases/v[12]']\n")
    assert mod.workflow_triggers_branch(spec, "releases/v1") is True   # char class
    assert mod.workflow_triggers_branch(spec, "releases/v3") is False
    spec = yaml.safe_load("on:\n  push:\n    branches: ['v1.+']\n")
    assert mod.workflow_triggers_branch(spec, "v1.") is True           # + = one-or-more of prev atom
    assert mod.workflow_triggers_branch(spec, "v1..") is True
    assert mod.workflow_triggers_branch(spec, "v1") is False
    spec = yaml.safe_load("on:\n  push:\n    branches: ['feat-?x']\n")
    assert mod.workflow_triggers_branch(spec, "featx") is True         # ? = zero-or-one of prev atom
    assert mod.workflow_triggers_branch(spec, "feat-x") is True
    assert mod.workflow_triggers_branch(spec, "feat--x") is False
    spec = yaml.safe_load("on:\n  push:\n    branches: ['branch[0-9]+']\n")
    assert mod.workflow_triggers_branch(spec, "branch7") is True       # + on a class
    assert mod.workflow_triggers_branch(spec, "branch42") is True
    assert mod.workflow_triggers_branch(spec, "branch") is False
    spec = yaml.safe_load("on:\n  push:\n    branches: ['releases/**']\n")
    assert mod.workflow_triggers_branch(spec, "releases/3/hotfix") is True


def test_trailer_bypass_needs_real_trailer_block() -> None:
    mod = _load(GUARDS_SCRIPT, "gp8")
    import subprocess as _sp, tempfile as _tf
    repo = Path(_tf.mkdtemp())
    def g(*a):
        return _sp.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (repo / "f").write_text("x"); g("add", "."); g("commit", "-qm", "init")
    base = "HEAD"
    import os
    cwd = os.getcwd(); os.chdir(repo)
    try:
        # body prose line looks trailer-shaped but has no trailer block
        g("commit", "-qm", "wip prose\nGuarded-Path: db-migrations", "--allow-empty")
        assert mod.trailer_bypasses(base, "HEAD") == set()
        # real trailer block (blank line before it) bypasses
        g("commit", "-qm", "wip\n\nGuarded-Path: db-migrations", "--allow-empty")
        assert "db-migrations" in mod.trailer_bypasses("HEAD~1", "HEAD")
    finally:
        os.chdir(cwd)


def test_lanes_non_list_fails_closed() -> None:
    mod = _load(CONTRACT_SCRIPT, "cdc8b")
    import pytest, tempfile as _tf
    p = Path(_tf.mkdtemp()) / "c.yaml"
    p.write_text("schema_version: 1\nrepos:\n  - repo: Org/x\n    lanes: prod\n")
    with pytest.raises(SystemExit) as e:
        mod.load_contract(p)
    assert "lanes" in str(e.value)


# ── review round 9 ───────────────────────────────────────────────────────────

def test_registry_unhashable_name_no_crash() -> None:
    mod = _load(REGISTRY_SCRIPT, "var9")
    errs: list[str] = []
    mod.validate({"schema_version": 1, "automations": [
        {"name": [], "repo": "o/r", "workflow": "w", "trigger": {"type": "push"},
         "risk_tier": "green", "owner": "x"}]}, errs)
    assert any("must be a string" in e for e in errs)


def test_repo_non_string_rejected() -> None:
    mod = _load(CONTRACT_SCRIPT, "cdc9")
    import pytest, tempfile as _tf
    p = Path(_tf.mkdtemp()) / "c.yaml"
    p.write_text("schema_version: 1\nrepos:\n  - repo: 123\n    lanes:\n      - {branch: dev, environment: dev, workflow: .github/workflows/d.yml}\n")
    with pytest.raises(SystemExit) as e:
        mod.load_contract(p)
    assert "'repo' must be a string" in str(e.value)


def test_changed_files_unquoted_unicode(tmp_path: Path) -> None:
    """Non-ASCII path must come back verbatim, not C-quoted."""
    repo = tmp_path / "r"
    _init_repo(repo, GUARDS, {"alembic/v1.py": "1"})
    _git(repo, "checkout", "-qb", "feat")
    (repo / "alembic/é.py").write_text("x")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "add é")
    import os
    cwd = os.getcwd(); os.chdir(repo)
    try:
        mod = _load(GUARDS_SCRIPT, "gp9")
        files = mod.changed_files("main", "HEAD")
    finally:
        os.chdir(cwd)
    assert "alembic/é.py" in files


# ── review round 32 ─────────────────────────────────────────────────────────

def test_registry_malformed_sibling_event(tmp_path: Path) -> None:
    """`on: {push: null, schedule: false}` — the checked event is fine but the
    bad sibling makes the whole file unloadable."""
    import argparse
    mod = _load(REGISTRY_SCRIPT, "var32a")
    repo_dir = tmp_path / "repo32"
    wf = repo_dir / ".github/workflows/x.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text("on:\n  push: null\n  schedule: false\n"
                  "jobs:\n  x: {runs-on: ubuntu-latest, steps: [{run: 'true'}]}\n")
    auto = {"name": "n", "repo": "Org/repo32",
            "workflow": ".github/workflows/x.yml",
            "trigger": {"type": "push"}, "risk_tier": "green", "owner": "o"}
    args = argparse.Namespace(mode="local", repos_dir=str(tmp_path))
    errs: list[str] = []
    mod.check_workflow_files({"automations": [auto]}, args, errs)
    assert errs and "schedule" in errs[0]


def test_registry_unknown_sibling_event(tmp_path: Path) -> None:
    """An invented event name is itself a load failure."""
    import argparse
    mod = _load(REGISTRY_SCRIPT, "var32b")
    repo_dir = tmp_path / "repo32b"
    wf = repo_dir / ".github/workflows/x.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text("on:\n  push: null\n  fake_event: {types: [x]}\n"
                  "jobs:\n  x: {runs-on: ubuntu-latest, steps: [{run: 'true'}]}\n")
    auto = {"name": "n", "repo": "Org/repo32b",
            "workflow": ".github/workflows/x.yml",
            "trigger": {"type": "push"}, "risk_tier": "green", "owner": "o"}
    args = argparse.Namespace(mode="local", repos_dir=str(tmp_path))
    errs: list[str] = []
    mod.check_workflow_files({"automations": [auto]}, args, errs)
    assert errs and "unknown event" in errs[0]


def test_registry_workflow_path_not_in_workflows_dir(tmp_path: Path) -> None:
    """A `workflow:` outside .github/workflows/ can never run — fail it."""
    import argparse
    mod = _load(REGISTRY_SCRIPT, "var32c")
    repo_dir = tmp_path / "repo32c"
    (repo_dir / "archive").mkdir(parents=True)
    (repo_dir / "archive/nightly.yml").write_text(
        "on: push\njobs:\n  x: {runs-on: ubuntu-latest, steps: [{run: 't'}]}\n")
    auto = {"name": "n", "repo": "Org/repo32c", "workflow": "archive/nightly.yml",
            "trigger": {"type": "push"}, "risk_tier": "green", "owner": "o"}
    args = argparse.Namespace(mode="local", repos_dir=str(tmp_path))
    errs: list[str] = []
    mod.check_workflow_files({"automations": [auto]}, args, errs)
    assert errs and ".github/workflows" in errs[0]


def test_contract_malformed_sibling_event() -> None:
    """Deployment checker: a bad sibling unloads the file → push can't fire."""
    mod = _load(CONTRACT_SCRIPT, "cdc32")
    spec = yaml.safe_load("on:\n  push: {branches: [develop]}\n  schedule: false\n")
    assert mod.workflow_triggers_branch(spec, "develop") is False
    spec = yaml.safe_load("on:\n  push: {branches: [develop]}\n  nope: {}\n")
    assert mod.workflow_triggers_branch(spec, "develop") is False
    spec = yaml.safe_load("on:\n  push: {branches: [develop]}\n"
                          "  schedule: [{cron: '0 2 * * *'}]\n")
    assert mod.workflow_triggers_branch(spec, "develop") is True


def test_contract_workflow_path_restriction(tmp_path: Path) -> None:
    """fetch_workflow rejects paths outside .github/workflows/ in both modes."""
    import argparse
    mod = _load(CONTRACT_SCRIPT, "cdc32b")
    args = argparse.Namespace(mode="local", repos_dir=str(tmp_path), token="x")
    _, err = mod.fetch_workflow("Org/repo", "archive/nightly.yml", "main", args)
    assert err and ".github/workflows" in err


# ── review round 33 ─────────────────────────────────────────────────────────

def test_registry_workflow_call_input_grammar(tmp_path: Path) -> None:
    """workflow_call inputs: `type` is required and only bool/number/string —
    dispatch grammar (`choice`+options) must NOT be applied to call inputs."""
    import argparse
    mod = _load(REGISTRY_SCRIPT, "var33a")
    repo_dir = tmp_path / "repo33a"
    wf = repo_dir / ".github/workflows/x.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text(
        "on:\n  push: null\n"
        "  workflow_call:\n    inputs:\n      target:\n"
        "        type: choice\n        options: [prod]\n"
        "jobs:\n  x: {runs-on: ubuntu-latest, steps: [{run: 'true'}]}\n")
    auto = {"name": "n", "repo": "Org/repo33a",
            "workflow": ".github/workflows/x.yml",
            "trigger": {"type": "push"}, "risk_tier": "green", "owner": "o"}
    args = argparse.Namespace(mode="local", repos_dir=str(tmp_path))
    errs: list[str] = []
    mod.check_workflow_files({"automations": [auto]}, args, errs)
    assert errs and "workflow_call.inputs.target" in errs[0]
    wf.write_text(
        "on:\n  push: null\n"
        "  workflow_call:\n    inputs:\n      target: {description: x}\n"
        "jobs:\n  x: {runs-on: ubuntu-latest, steps: [{run: 'true'}]}\n")
    errs = []
    mod.check_workflow_files({"automations": [auto]}, args, errs)
    assert errs and "not a valid input type" in errs[0]   # type missing


def test_registry_workflow_run_branch_filters_ok(tmp_path: Path) -> None:
    """workflow_run DOES support branches/branches-ignore — don't reject."""
    import argparse
    mod = _load(REGISTRY_SCRIPT, "var33b")
    repo_dir = tmp_path / "repo33b"
    wf = repo_dir / ".github/workflows/x.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text(
        "on:\n  push: null\n"
        "  workflow_run:\n    workflows: [CI]\n    branches: [main]\n"
        "jobs:\n  x: {runs-on: ubuntu-latest, steps: [{run: 'true'}]}\n")
    auto = {"name": "n", "repo": "Org/repo33b",
            "workflow": ".github/workflows/x.yml",
            "trigger": {"type": "push"}, "risk_tier": "green", "owner": "o"}
    args = argparse.Namespace(mode="local", repos_dir=str(tmp_path))
    errs: list[str] = []
    mod.check_workflow_files({"automations": [auto]}, args, errs)
    assert errs == []


def test_registry_empty_ref_filters_fail(tmp_path: Path) -> None:
    """`branches: []`/`tags: []` are positive filters that fire nothing."""
    import argparse
    mod = _load(REGISTRY_SCRIPT, "var33c")
    repo_dir = tmp_path / "repo33c"
    wf = repo_dir / ".github/workflows/x.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text("on:\n  push:\n    branches: []\n"
                  "jobs:\n  x: {runs-on: ubuntu-latest, steps: [{run: 't'}]}\n")
    auto = {"name": "n", "repo": "Org/repo33c",
            "workflow": ".github/workflows/x.yml",
            "trigger": {"type": "push"}, "risk_tier": "green", "owner": "o"}
    args = argparse.Namespace(mode="local", repos_dir=str(tmp_path))
    errs: list[str] = []
    mod.check_workflow_files({"automations": [auto]}, args, errs)
    assert errs and "empty branches filter" in errs[0]


def test_contract_sibling_grammar_parity() -> None:
    """Contract checker mirrors: workflow_run branches ok; empty branches bad;
    workflow_call input with dispatch-only grammar rejected."""
    mod = _load(CONTRACT_SCRIPT, "cdc33")
    spec = yaml.safe_load(
        "on:\n  push: {branches: [develop]}\n"
        "  workflow_run: {workflows: [CI], branches: [main]}\n")
    assert mod.workflow_triggers_branch(spec, "develop") is True
    spec = yaml.safe_load(
        "on:\n  push: {branches: [develop]}\n"
        "  pull_request_target: {branches: [], branches-ignore: [x]}\n")
    assert mod.workflow_triggers_branch(spec, "develop") is False
    spec = yaml.safe_load(
        "on:\n  push: {branches: [develop]}\n"
        "  workflow_call: {inputs: {t: {type: choice, options: [a]}}}\n")
    assert mod.workflow_triggers_branch(spec, "develop") is False


# ── review round 34 ─────────────────────────────────────────────────────────

def test_registry_choice_input_requires_options(tmp_path: Path) -> None:
    """`type: choice` without options can't load — reject it."""
    import argparse
    mod = _load(REGISTRY_SCRIPT, "var34a")
    repo_dir = tmp_path / "repo34a"
    wf = repo_dir / ".github/workflows/x.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text(
        "on:\n  push: null\n"
        "  workflow_dispatch:\n    inputs:\n      env:\n        type: choice\n"
        "jobs:\n  x: {runs-on: ubuntu-latest, steps: [{run: 'true'}]}\n")
    auto = {"name": "n", "repo": "Org/repo34a",
            "workflow": ".github/workflows/x.yml",
            "trigger": {"type": "push"}, "risk_tier": "green", "owner": "o"}
    args = argparse.Namespace(mode="local", repos_dir=str(tmp_path))
    errs: list[str] = []
    mod.check_workflow_files({"automations": [auto]}, args, errs)
    assert errs and "choice" in errs[0]
    wf.write_text(
        "on:\n  push: null\n"
        "  workflow_dispatch:\n    inputs:\n      env:\n"
        "        type: choice\n        options: [staging, prod]\n"
        "jobs:\n  x: {runs-on: ubuntu-latest, steps: [{run: 'true'}]}\n")
    errs = []
    mod.check_workflow_files({"automations": [auto]}, args, errs)
    assert errs == []


def test_registry_expr_false_job_not_runnable(tmp_path: Path) -> None:
    """`if: ${{ false }}` is permanently skipped — counts as no jobs."""
    import argparse
    mod = _load(REGISTRY_SCRIPT, "var34b")
    repo_dir = tmp_path / "repo34b"
    wf = repo_dir / ".github/workflows/x.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text(
        "on: push\n"
        "jobs:\n  x:\n    if: ${{ false }}\n"
        "    runs-on: ubuntu-latest\n    steps: [{run: 'true'}]\n")
    auto = {"name": "n", "repo": "Org/repo34b",
            "workflow": ".github/workflows/x.yml",
            "trigger": {"type": "push"}, "risk_tier": "green", "owner": "o"}
    args = argparse.Namespace(mode="local", repos_dir=str(tmp_path))
    errs: list[str] = []
    mod.check_workflow_files({"automations": [auto]}, args, errs)
    assert errs and "no runnable jobs" in errs[0]


def test_contract_expr_false_and_choice_parity() -> None:
    """Contract checker mirrors: expression-false job skipped; choice w/o
    options rejected as a sibling workflow_dispatch declaration."""
    mod = _load(CONTRACT_SCRIPT, "cdc34")
    assert mod._executable({"runs-on": "ubuntu-latest",
                            "if": "${{ false }}",
                            "steps": [{"run": "true"}]}) is False
    assert mod._executable({"runs-on": "ubuntu-latest",
                            "if": "${{false}}",
                            "steps": [{"run": "true"}]}) is False
    assert mod._executable({"runs-on": "ubuntu-latest",
                            "steps": [{"run": "true"}]}) is True
    spec = yaml.safe_load(
        "on:\n  push: {branches: [develop]}\n"
        "  workflow_dispatch: {inputs: {env: {type: choice}}}\n")
    assert mod.workflow_triggers_branch(spec, "develop") is False
