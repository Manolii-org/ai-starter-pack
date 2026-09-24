import json
import subprocess
import sys
from pathlib import Path

import importlib.util

SCRIPTS = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_branch_protection", SCRIPTS / "check-branch-protection.py")
cbp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cbp)

CONTRACT = """\
schema_version: 1
repos:
  - repo: Org/app
    default_branch: main
    required_checks: [guards, scan]
    protected_branches: [develop]
    lanes:
      - branch: main
        environment: production
        workflow: .github/workflows/deploy.yml
        required_checks: [smoke]
      - branch: staging
        environment: uat
        workflow: .github/workflows/deploy.yml
        rollback_required: false
"""


def _write_contract(tmp_path: Path) -> Path:
    p = tmp_path / "contract.yaml"
    p.write_text(CONTRACT)
    return p


def _fixture(fixtures: Path, repo: str, branch: str, protection: dict | None) -> None:
    base = fixtures / cbp.fixture_name(repo, branch)
    if protection is None:
        base.with_suffix(".404").touch()
    else:
        base.with_suffix(".json").write_text(json.dumps(protection))


def _prot(contexts: list[str]) -> dict:
    """New API shape: checks[] entries with context + app_id."""
    return {
        "required_status_checks": {
            "url": "https://api/x", "strict": True,
            "checks": [{"context": c, "app_id": None} for c in contexts],
            "contexts": contexts,
        },
        "enforce_admins": {"url": "https://api/x", "enabled": True},
        "required_pull_request_reviews": {"url": "https://api/x",
                                        "required_approving_review_count": 1},
        "restrictions": {"users": [], "teams": [], "apps": []},
        "required_signatures": {"url": "https://api/x", "enabled": True},
        "allow_force_pushes": {"enabled": False},
    }


def test_current_contexts_new_shape() -> None:
    assert cbp.current_contexts(_prot(["guards", "scan"])) == ["guards", "scan"]


def test_current_contexts_legacy_shape() -> None:
    assert cbp.current_contexts({"required_status_checks": {"contexts": ["a"]}}) == ["a"]


def test_audited_lane_overrides_repo_level() -> None:
    spec = {"required_checks": ["guards", "scan"],
            "protected_branches": ["develop"],
            "lanes": [{"branch": "main", "required_checks": ["smoke"]},
                      {"branch": "staging"},
                      {"branch": "prod", "required_checks": []}]}
    assert cbp.audited(spec) == {
        "develop": ["guards", "scan"],
        "main": ["smoke"],
        "staging": ["guards", "scan"],
        "prod": [],
    }


def test_fix_body_preserves_existing_settings() -> None:
    body = cbp.fix_body(_prot(["guards"]), ["guards", "scan"])
    assert body["enforce_admins"] is True
    # required_signatures is not a PUT-body field (own endpoint) — dropped.
    assert "required_signatures" not in body
    assert body["required_pull_request_reviews"]["required_approving_review_count"] == 1
    checks = body["required_status_checks"]["checks"]
    assert [c["context"] for c in checks] == ["guards", "scan"]
    assert body["required_status_checks"]["strict"] is True


def test_fix_body_skeleton_for_unprotected_branch() -> None:
    body = cbp.fix_body(None, ["guards", "scan"])
    assert body["required_status_checks"]["checks"] == [
        {"context": "guards"}, {"context": "scan"}]
    # Unprotected-branch payloads must still carry safe merge settings —
    # a checks-only PUT would leave merges review-free.
    assert body["required_pull_request_reviews"]["required_approving_review_count"] == 1


def _run(contract: Path, fixtures: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "check-branch-protection.py"),
         str(contract), "--fixtures", str(fixtures), *extra],
        capture_output=True, text=True, check=False)


def test_e2e_conform_and_missing(tmp_path: Path) -> None:
    contract = _write_contract(tmp_path)
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    _fixture(fixtures, "Org/app", "develop", _prot(["guards", "scan"]))
    _fixture(fixtures, "Org/app", "main", _prot(["guards", "scan", "smoke"]))
    _fixture(fixtures, "Org/app", "staging", _prot(["guards"]))  # missing scan
    out = _run(contract, fixtures)
    assert out.returncode == 1
    assert "Org/app@staging" in out.stdout and "scan" in out.stdout
    out = _run(contract, fixtures, "--warn-only")
    assert out.returncode == 0


def test_e2e_unprotected_branch_reports_all_missing(tmp_path: Path) -> None:
    contract = _write_contract(tmp_path)
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    _fixture(fixtures, "Org/app", "develop", None)  # 404
    _fixture(fixtures, "Org/app", "main", _prot(["guards", "scan", "smoke"]))
    _fixture(fixtures, "Org/app", "staging", _prot(["guards", "scan"]))
    fixes_dir = tmp_path / "fixes"
    out = _run(contract, fixtures, "--emit-fixes", str(fixes_dir))
    assert out.returncode == 1
    body = json.loads(
        (fixes_dir / f"{cbp.fixture_name('Org/app', 'develop')}.json").read_text())
    assert {c["context"] for c in body["required_status_checks"]["checks"]} == {
        "guards", "scan"}
    assert "gh api -X PUT" in (fixes_dir / "apply-fixes.md").read_text()


def test_e2e_no_checks_declared_is_clean(tmp_path: Path) -> None:
    c = tmp_path / "c.yaml"
    c.write_text("schema_version: 1\nrepos:\n  - repo: Org/app\n"
                 "    lanes:\n      - branch: main\n        environment: production\n"
                 "        workflow: .github/workflows/deploy.yml\n")
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    assert _run(c, fixtures).returncode == 0


def test_e2e_empty_declared_checks_unprotected_is_finding(tmp_path: Path) -> None:
    """required_checks: [] means 'nothing required' — an unprotected branch is
    still a finding (verified-empty vs skipped are different outcomes)."""
    c = tmp_path / "c.yaml"
    c.write_text("schema_version: 1\nrepos:\n  - repo: Org/app\n"
                 "    lanes:\n      - branch: main\n        environment: production\n"
                 "        workflow: .github/workflows/deploy.yml\n"
                 "        required_checks: []\n")
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    _fixture(fixtures, "Org/app", "main", None)  # 404
    out = _run(c, fixtures)
    assert out.returncode == 1
    assert "unprotected" in out.stdout


def test_fix_body_omits_null_app_id() -> None:
    """GET shapes without a checks[] array (or with app_id: null) must emit
    context-only entries — a null app_id fails the PUT with 422."""
    prot = {"required_status_checks": {"strict": False,
                                       "contexts": ["guards"]}}
    body = cbp.fix_body(prot, ["guards", "scan"])
    checks = body["required_status_checks"]["checks"]
    assert all("app_id" not in c or isinstance(c["app_id"], int) for c in checks)
    assert {c["context"] for c in checks} == {"guards", "scan"}
