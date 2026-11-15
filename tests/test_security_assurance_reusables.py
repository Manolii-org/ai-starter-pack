"""Contracts for the security-assurance changes to the shared reusables.

Covers: archived semgrep-action removal, Semgrep image pinning, the
workflow-security runner/change-detection inputs, the secret-scan engine and
OSV options (with v1 defaults and job names preserved), the skill-scan gate,
and the staged-secret pre-commit hook.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WF / name).read_text(encoding="utf-8"))


def _inputs(doc: dict) -> dict:
    # PyYAML parses the bare `on:` key as boolean True.
    on = doc.get("on", doc.get(True))
    return on["workflow_call"]["inputs"]


def test_archived_semgrep_action_is_gone_from_static_review():
    # 1.27.x scope: the static-review pair. PR Assessment's own Semgrep step
    # moves off the archived action in 2.0.0 together with its wider rework.
    offenders = [
        name
        for name in ("static-review.yml", "static-review-reusable.yml")
        if "uses: returntocorp/semgrep-action" in (WF / name).read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_semgrep_runs_in_digest_pinned_image_in_both_static_reviews():
    images = set()
    for name in ("static-review.yml", "static-review-reusable.yml"):
        job = _load(name)["jobs"]["semgrep"]
        image = job["container"]["image"]
        assert re.fullmatch(r"semgrep/semgrep:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", image), image
        images.add(image)
        script = "\n".join(s.get("run", "") for s in job["steps"])
        assert "--baseline-commit" in script
        assert "--metrics=off" in script
        assert "--error" in script
    assert len(images) == 1, "static-review.yml and the reusable must pin the same Semgrep image"


def test_static_review_reusable_semgrep_defaults_preserve_v1_rulesets():
    inputs = _inputs(_load("static-review-reusable.yml"))
    assert inputs["semgrep_config"]["default"].split() == ["p/default", "p/secrets", "p/owasp-top-ten"]
    assert inputs["semgrep_local_rules"]["default"] == ""
    assert inputs["semgrep_runs_on"]["default"] == ""
    job = _load("static-review-reusable.yml")["jobs"]["semgrep"]
    assert job["runs-on"] == "${{ inputs.semgrep_runs_on || inputs.runs_on }}"


def test_workflow_security_runner_and_change_detection():
    doc = _load("workflow-security-reusable.yml")
    inputs = _inputs(doc)
    assert inputs["runs_on"]["default"] == "ubuntu-latest"
    assert inputs["changed_only"]["default"] is False
    job = doc["jobs"]["scan"]
    assert job["name"] == "Workflow security"  # required-check name unchanged
    assert job["runs-on"] == "${{ inputs.runs_on }}"
    names = [s.get("name", "") for s in job["steps"]]
    detect = names.index("Detect workflow changes")
    for step in job["steps"][detect + 1:]:
        assert step.get("if") == "steps.changes.outputs.relevant == 'true'", step.get("name")
    detect_src = job["steps"][detect]["run"]
    assert "relevant = \"true\"" in detect_src  # fail-safe default is to scan
    assert "len(files) < 300" in detect_src


def test_secret_scan_engine_and_osv_keep_v1_contract():
    doc = _load("secret-scan-reusable.yml")
    inputs = _inputs(doc)
    assert inputs["engine"]["default"] == "gitleaks"
    assert inputs["osv_scanner"]["default"] == "off"
    assert re.fullmatch(r"[0-9a-f]{64}", inputs["trufflehog_sha256"]["default"])
    on = doc.get("on", doc.get(True))
    assert set(on["workflow_call"]["outputs"]) == {"npm_audit_activation", "pip_audit_activation"}
    jobs = doc["jobs"]
    # Job names are required-check names in consumer rulesets.
    assert jobs["gitleaks"]["name"] == "Detect Secrets"
    assert jobs["npm-audit"]["name"] == "Dependency Vulnerability Audit"
    assert jobs["pip-audit"]["name"] == "Python Dependency Audit"
    for step in jobs["gitleaks"]["steps"]:
        name = step.get("name", "")
        if "Gitleaks" in name or "gitleaks" in name:
            assert "inputs.engine == 'gitleaks'" in step.get("if", ""), name
        if "TruffleHog" in name:
            assert "inputs.engine == 'trufflehog'" in step.get("if", ""), name
    install = next(s for s in jobs["gitleaks"]["steps"] if s.get("name", "").startswith("Install TruffleHog"))
    assert "sha256sum -c" in install["run"]
    osv = next(s for s in jobs["npm-audit"]["steps"] if s.get("id") == "osv")
    assert osv["if"] == "${{ inputs.osv_scanner != 'off' }}"
    assert "sha256sum -c" in osv["run"]


def _gate():
    spec = importlib.util.spec_from_file_location("skill_scan_gate", ROOT / "scripts" / "skill-scan-gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_skill_scan_gate_thresholds_and_fail_closed():
    gate = _gate()
    ok = {"execution_successful": True, "skills": [
        {"name": "a", "risk_assessment": {"severity": "LOW", "score": 3}},
        {"name": "b", "risk_assessment": {"severity": "MEDIUM", "score": 40}},
    ]}
    assert gate.evaluate(ok, "HIGH")[0] == 0
    high = {"execution_successful": True, "skills": [{"name": "c", "risk_assessment": {"severity": "HIGH", "score": 70}}]}
    assert gate.evaluate(high, "HIGH")[0] == 1
    assert gate.evaluate({"execution_successful": False, "skills": []}, "HIGH")[0] == 2
    unknown = {"skills": [{"name": "d", "risk_assessment": {"severity": "WEIRD"}}]}
    assert gate.evaluate(unknown, "HIGH")[0] == 1  # unknown severity fails closed
    assert gate.evaluate({"name": "single", "risk_severity": "LOW"}, "HIGH")[0] == 0


def test_skill_scan_is_pack_only_and_static():
    text = (WF / "skill-scan.yml").read_text(encoding="utf-8")
    assert "--no-llm" in text
    assert re.search(r"SKILLSPECTOR_REF: [0-9a-f]{40} # v\d+\.\d+\.\d+", text)
    copier = (ROOT / "copier.yml").read_text(encoding="utf-8")
    assert "- .github/workflows/skill-scan.yml" in copier


def test_pre_commit_secret_scan_is_optional_and_local():
    hook = (ROOT / ".githooks" / "pre-commit").read_text(encoding="utf-8")
    assert "command -v gitleaks" in hook
    assert "--staged" in hook
    assert "SKIP_GITLEAKS" in hook


def test_coderabbit_round1_regressions():
    """CodeRabbit review on #341: five verified findings."""
    secret = (WF / "secret-scan-reusable.yml").read_text(encoding="utf-8")
    # TruffleHog must fail on recorded scan errors, not only findings.
    assert "--fail-on-scan-errors" in secret
    # OSV runs before the inert decision and is consulted by it.
    jobs = _load("secret-scan-reusable.yml")["jobs"]["npm-audit"]["steps"]
    names = [s.get("name", "") for s in jobs]
    osv_i = next(i for i, n in enumerate(names) if n.startswith("OSV-Scanner"))
    detect_i = names.index("Detect npm lockfile")
    assert osv_i < detect_i
    assert "OSV_AUDITED" in jobs[detect_i]["env"]
    assert 'echo "audited=true"' in jobs[osv_i]["run"]
    # Skill scan covers agents and evaluates the report on SkillSpector exit 1.
    skill = (WF / "skill-scan.yml").read_text(encoding="utf-8")
    assert ".claude/agents" in skill and "plugin/manolii-framework/agents" in skill
    assert "set +e" in skill and '"$rc" -ge 2' in skill
    # Pack-only tests are not rendered into consumers.
    assert "- tests/test_security_assurance_reusables.py" in (ROOT / "copier.yml").read_text(encoding="utf-8")


def _routing_detect_script() -> str:
    for step in _load("static-review-reusable.yml")["jobs"]["agent-routing-lint"]["steps"]:
        if step.get("id") == "detect":
            return step["run"]
    raise AssertionError("detect step not found")


def test_routing_lint_skips_callers_without_a_routing_config(tmp_path):
    import subprocess

    script = _routing_detect_script()

    def found(*paths):
        root = tmp_path / str(len(list(tmp_path.iterdir())))
        root.mkdir()
        for p in paths:
            (root / p).parent.mkdir(parents=True, exist_ok=True)
            (root / p).write_text("x")
        out = root / "out"
        subprocess.run(["bash", "-c", script], cwd=root, env={"GITHUB_OUTPUT": str(out), "PATH": "/usr/bin:/bin"}, check=True)
        return out.read_text().strip()

    assert found() == "found=false"
    assert found(".claude/skills/a/SKILL.md") == "found=false"
    assert found(".claude/skills/a/SKILL.md", "scripts/lint-agent-routing.py") == "found=false"
    assert found(".claude/agents/x.md", "scripts/lint-agent-routing.py", ".claude/model-routing.json") == "found=true"
    assert found(".claude/skills/a/SKILL.md", "scripts/lint-agent-routing.py", ".claude/model-routing.json") == "found=true"


def _semgrep_script(name):
    job = _load(name)["jobs"]["semgrep"]
    return "\n".join(s["run"] for s in job["steps"] if "semgrep" in s.get("run", ""))


def test_semgrep_baseline_is_the_merge_commits_base_not_a_stale_base_sha(tmp_path):
    """A base branch that moved after the PR event must not be blamed on the PR."""
    import os
    import subprocess

    def git(*a, cwd):
        return subprocess.run(["git", *a], cwd=cwd, env=env_git, check=True, capture_output=True, text=True).stdout.strip()

    repo = tmp_path / "repo"
    repo.mkdir()
    env_git = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@x", "HOME": str(tmp_path), "PATH": os.environ["PATH"]}
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "a").write_text("1")
    git("add", "a", cwd=repo); git("commit", "-qm", "base", cwd=repo)
    stale_base = git("rev-parse", "HEAD", cwd=repo)
    git("checkout", "-qb", "pr", cwd=repo)
    (repo / "b").write_text("pr")
    git("add", "b", cwd=repo); git("commit", "-qm", "pr", cwd=repo)
    git("checkout", "-q", "main", cwd=repo)
    (repo / "c").write_text("base moved")
    git("add", "c", cwd=repo); git("commit", "-qm", "moved", cwd=repo)
    moved_base = git("rev-parse", "HEAD", cwd=repo)
    git("merge", "-q", "--no-ff", "-m", "test merge", "pr", cwd=repo)  # GitHub's refs/pull/N/merge shape

    bindir = tmp_path / "bin"
    bindir.mkdir()
    args_file = tmp_path / "args"
    (bindir / "semgrep").write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {args_file}\n")
    (bindir / "semgrep").chmod(0o755)
    for name in ("static-review.yml", "static-review-reusable.yml"):
        env = {**env_git, "PATH": f"{bindir}:{os.environ['PATH']}", "GITHUB_WORKSPACE": str(repo),
               "PR_BASE_SHA": stale_base, "MQ_BASE_SHA": "", "SEMGREP_CONFIG": "p/default", "SEMGREP_LOCAL_RULES": ""}
        subprocess.run(["sh", "-c", _semgrep_script(name)], cwd=repo, env=env, check=True)
        args = args_file.read_text().split()
        assert args[args.index("--baseline-commit") + 1] == moved_base, name
