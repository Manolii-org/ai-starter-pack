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


def test_archived_semgrep_action_is_gone_everywhere():
    offenders = [
        str(p.relative_to(ROOT))
        for p in list(WF.glob("*.yml")) + list(WF.glob("*.yaml"))
        if "semgrep-action" in p.read_text(encoding="utf-8")
        and "uses: returntocorp/semgrep-action" in p.read_text(encoding="utf-8")
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
