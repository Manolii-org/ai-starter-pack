"""Evidence receipts must fail closed without inventing savings or assurance."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".claude/skills/security-audit-evidence/scripts/audit_evidence.py"
SPEC = importlib.util.spec_from_file_location("audit_evidence", SCRIPT)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)
REV = "a" * 40


def receipt():
    return {
        "schema": "security-audit-evidence/v1", "owner": "Example-org",
        "source_revisions": [{"repository": "Example-org/api", "revision": REV, "dirty": False}],
        "budget": {"max_invocations": 4, "used_invocations": 2},
        "execution": {"mode": "source_only", "production_access": False, "secret_environment": False},
        "coverage": [{"id": "auth-1", "repository": "Example-org/api", "revision": REV,
                      "boundary": "anonymous to tenant", "attack_class": "authorization",
                      "status": "covered", "evidence": ["private/trace-1"]}],
        "findings": [{"unit_id": "auth-1", "verdict": "confirmed", "hunter_id": "hunter-1",
                      "verifier_id": "verifier-2", "validation": "source_trace", "evidence": ["private/check-1"]}],
    }


def scan(sid, at, **metrics):
    return {"id": sid, "repository": "Example-org/api", "started_at": at,
            "status": "completed", "success": True, **metrics}


def metadata(rows):
    return {"schema": "security-scan-metadata/v1", "as_of": "2026-10-31T00:00:00Z",
            "history_complete_after": "2026-10-01T00:00:00Z", "scans": rows}


def compare(doc, **kwargs):
    args = dict(baseline="2026-10-09", comparison="2026-10-20", days=11, timezone="Australia/Sydney")
    args.update(kwargs)
    return audit.compare_metadata(doc, **args)


def test_receipt_attests_declared_scope_without_claiming_correctness():
    result = audit.validate_receipt(receipt())
    assert result["coverage_complete"] is True
    assert result["vulnerability_correctness_verified"] is False
    assert "private/trace-1" not in json.dumps(result)


@pytest.mark.parametrize("change", [
    lambda d: d["source_revisions"][0].update(dirty=True),
    lambda d: d["source_revisions"][0].update(repository="Other-org/api"),
    lambda d: d["coverage"][0].update(revision="b" * 40),
    lambda d: d["coverage"][0].update(revision=None),
    lambda d: d["coverage"][0].update(evidence=[]),
    lambda d: d["budget"].update(used_invocations=5),
    lambda d: d["execution"].update(production_access=True),
    lambda d: d["execution"].update(secret_environment=True),
    lambda d: d["findings"][0].update(verifier_id="hunter-1"),
    lambda d: d["findings"][0].update(unit_id="missing"),
    lambda d: d["findings"][0].update(validation="sandbox_reproducer"),
])
def test_unsafe_or_stale_receipts_are_refused(change):
    doc = receipt()
    change(doc)
    with pytest.raises(ValueError):
        audit.validate_receipt(doc)


@pytest.mark.parametrize("status", ["needs_validation", "deferred", "out_of_scope"])
def test_uncovered_scope_is_visible(status):
    doc = receipt()
    doc["coverage"][0]["status"] = status
    assert not audit.validate_receipt(doc)["coverage_complete"]


def test_runtime_validation_requires_verified_networkless_isolation():
    doc = receipt()
    doc["findings"][0]["validation"] = "sandbox_reproducer"
    doc["execution"].update(mode="isolated", isolation_verified=True, network_disabled=True)
    assert audit.validate_receipt(doc)["findings"] == {"confirmed": 1}
    doc["execution"]["network_disabled"] = False
    with pytest.raises(ValueError):
        audit.validate_receipt(doc)


def test_unresolved_finding_keeps_coverage_incomplete():
    doc = receipt()
    doc["findings"][0]["verdict"] = "needs_validation"
    assert not audit.validate_receipt(doc)["coverage_complete"]


def test_counts_do_not_become_money_when_usage_is_absent():
    rows = [scan("1", "2026-10-10T00:00:00Z"), scan("2", "2026-10-11T00:00:00Z"),
            scan("3", "2026-10-21T00:00:00Z")]
    result = compare(metadata(rows))
    owner = result["owners"]["Example-org"]
    assert owner["scan_start_reduction_percent"] == 50.0
    assert owner["observed_spend_reduction_usd"] is None
    assert result["attributed_savings_usd"] is None
    assert owner["baseline"]["analyst_minutes"] is None


def test_explicit_zero_is_a_measurement_but_not_attribution():
    rows = [scan("1", "2026-10-10T00:00:00Z", recorded_cost_usd="4.10", input_tokens=10),
            scan("2", "2026-10-21T00:00:00Z", recorded_cost_usd="0", input_tokens=0)]
    result = compare(metadata(rows))
    owner = result["owners"]["Example-org"]
    assert owner["observed_spend_reduction_usd"] == "4.10"
    assert owner["comparison"]["recorded_cost_usd"] == "0"
    assert result["attribution"] == "not_measured"


def test_partial_metrics_and_incomplete_history_never_return_complete_totals():
    rows = [scan("1", "2026-10-10T00:00:00Z", recorded_cost_usd=2),
            scan("2", "2026-10-11T00:00:00Z"), scan("3", "2026-10-21T00:00:00Z", recorded_cost_usd=1)]
    doc = metadata(rows)
    result = compare(doc)["owners"]["Example-org"]
    assert result["baseline"]["recorded_cost_usd"] is None
    assert result["baseline"]["recorded_cost_usd_observed_scans"] == 1
    doc["history_complete_after"] = "2026-10-10T00:00:00Z"
    result = compare(doc)["owners"]["Example-org"]
    assert result["scan_start_reduction_percent"] is None
    assert result["observed_spend_reduction_usd"] is None


def test_duplicates_are_collapsed_and_conflicts_fail_closed():
    row = scan("1", "2026-10-10T00:00:00Z")
    assert compare(metadata([row, copy.deepcopy(row)]))["owners"]["Example-org"]["baseline"]["scan_starts"] == 1
    changed = dict(row, recorded_cost_usd=1)
    with pytest.raises(ValueError):
        compare(metadata([row, changed]))


def test_github_owner_case_does_not_split_measurements():
    rows = [scan("1", "2026-10-10T00:00:00Z"),
            scan("2", "2026-10-21T00:00:00Z", repository="example-org/worker")]
    result = compare(metadata(rows))["owners"]
    assert len(result) == 1
    assert next(iter(result.values()))["comparison"]["scan_starts"] == 1


def test_calendar_windows_follow_dst_and_exclude_end_boundary():
    rows = [scan("1", "2026-10-03T14:00:00Z"), scan("2", "2026-10-04T13:00:00Z")]
    result = compare(metadata(rows), baseline="2026-10-04", comparison="2026-10-05", days=1)
    before = result["owners"]["Example-org"]["baseline"]
    assert before["scan_starts"] == 1
    assert before["start"].endswith("+10:00")
    assert before["end_exclusive"].endswith("+11:00")


@pytest.mark.parametrize("metrics", [
    {"recorded_cost_usd": "NaN"}, {"recorded_cost_usd": -1}, {"recorded_cost_usd": True},
    {"input_tokens": 0.5}, {"status": None}, {"success": "true"},
    {"finished_at": "2026-10-09T00:00:00Z"},
])
def test_malformed_measurements_are_refused(metrics):
    with pytest.raises(ValueError):
        compare(metadata([scan("1", "2026-10-10T00:00:00Z", **metrics)]))


def test_cli_does_not_echo_untrusted_parse_errors_or_write_files(tmp_path):
    path = tmp_path / "receipt.json"
    path.write_text('{"private_do_not_echo": invalid}', encoding="utf-8")
    before = set(tmp_path.iterdir())
    run = subprocess.run([sys.executable, str(SCRIPT), "validate", str(path)],
                         capture_output=True, text=True, timeout=10)
    assert run.returncode == 2
    assert "private_do_not_echo" not in run.stderr + run.stdout
    assert set(tmp_path.iterdir()) == before


def test_generated_skill_installs_with_its_helper_and_runs_outside_the_pack(tmp_path):
    relative = Path("security-audit-evidence")
    canonical = ROOT / ".claude/skills" / relative
    registry = ROOT / "registry/platform/framework/skills" / relative
    plugin = ROOT / "plugin/manolii-framework/skills" / relative
    for leaf in ("SKILL.md", "contract.md", "scripts/audit_evidence.py"):
        assert (canonical / leaf).read_bytes() == (registry / leaf).read_bytes()
        assert (canonical / leaf).read_bytes() == (plugin / leaf).read_bytes()
    # A small real registry keeps this installation test bounded while using
    # exactly the generated capability bytes and the production resolver.
    source = tmp_path / "source"
    pack = source / "registry/platform/framework"
    (pack / ".claude-plugin").mkdir(parents=True)
    (pack / ".claude-plugin/plugin.json").write_text('{"name":"framework","version":"1.0.0"}')
    shutil.copytree(registry, pack / "skills" / relative)
    (source / "registry/plugins.json").write_text(json.dumps({"plugins": [{
        "scope": "platform", "name": "framework", "path": "registry/platform/framework"}]}))
    target = tmp_path / "consumer"
    target.mkdir()
    (target / "ai-manifest.yaml").write_text(
        'version: 1\nuniverse: example\nrequires:\n  - plugin: platform/framework\n    ref: "1.0.0"\n'
        'surfaces: [claude-code]\n')
    result = subprocess.run([sys.executable, str(ROOT / "scripts/ai-resolve.py"),
                             "--repo-root", str(target), "--registry", str(source), "--apply"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    installed = target / ".claude/skills" / relative
    assert (installed / "contract.md").read_bytes() == (canonical / "contract.md").read_bytes()
    data = tmp_path / "receipt.json"
    data.write_text(json.dumps(receipt()), encoding="utf-8")
    result = subprocess.run([sys.executable, "-I", "-B", str(installed / "scripts/audit_evidence.py"),
                             "validate", str(data)], cwd=target,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["coverage_complete"] is True
