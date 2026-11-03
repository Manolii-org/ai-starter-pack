"""Account consent, nomination, funding and cross-repository lock boundaries."""

import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

import pytest

if sys.platform != "linux":
    pytest.skip(
        "persistent review profiles require a Linux host", allow_module_level=True
    )

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/ci"))
import codex_review_profiles as profiles  # noqa: E402
import codex_review_worker as worker  # noqa: E402
from codex_review_models import ESCALATION, effort  # noqa: E402

ACCOUNT = hashlib.sha256(b"personal-account").hexdigest()
OTHER = hashlib.sha256(b"other-personal-account").hexdigest()
MACHINE = hashlib.sha256(Path("/etc/machine-id").read_bytes().strip()).hexdigest()


@pytest.fixture
def policy(monkeypatch):
    data = {
        "schema": "codex-review-profiles/v1",
        "host_sha256": MACHINE,
        "runner_access": {
            "mode": "selected_trusted_workflows",
            "verification_sha256": "a" * 64,
            "verified_until": int(time.time()) + 3600,
            "workflows": [
                "example/project/.github/workflows/codex-review-worker.yml@refs/heads/main",
                "example/special/.github/workflows/codex-review-worker.yml@refs/heads/main",
            ],
        },
        "profiles": {
            "alice": {"owner": "alice", "account_sha256": ACCOUNT, "generation": 1},
            "bob": {"owner": "bob", "account_sha256": OTHER, "generation": 1},
        },
        "repositories": {"example/special": "bob"},
        "organisations": {"example": "alice"},
    }
    consents = {}
    for name, account in (("alice", ACCOUNT), ("bob", OTHER)):
        consents[name] = {
            "schema": "codex-review-consent/v1",
            "profile": name,
            "owner": name,
            "account_sha256": account,
            "generation": 1,
            "revoked": False,
            "expires_at": int(time.time()) + 3600,
            "repositories": ["example/project", "example/special"],
            "lanes": ["code", "security"],
            "purchased_credits_disabled": True,
            "automatic_purchases_disabled": True,
        }
    monkeypatch.setattr(
        profiles,
        "protected_json",
        lambda path: copy.deepcopy(
            data if path == profiles.REGISTRY else consents[path.stem]
        ),
    )
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF",
        "example/project/.github/workflows/codex-review-worker.yml@refs/heads/main",
    )
    return data, consents


def test_repo_pin_overrides_org_and_case_is_canonical(policy, monkeypatch):
    assert profiles.select("Example/Project", "code")["profile"] == "alice"
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF",
        "example/special/.github/workflows/codex-review-worker.yml@refs/heads/main",
    )
    assert profiles.select("example/special", "security")["profile"] == "bob"


def test_unassigned_org_cannot_borrow_another_account(policy):
    with pytest.raises(profiles.ProfileError, match="host_access_unverified"):
        profiles.select("other/project", "code")


@pytest.mark.parametrize(
    "key,value",
    [
        ("revoked", True),
        ("expires_at", 0),
        ("generation", 2),
        ("owner", "bob"),
        ("account_sha256", OTHER),
        ("repositories", ["other/project"]),
        ("lanes", ["code"]),
        ("purchased_credits_disabled", False),
        ("automatic_purchases_disabled", False),
    ],
)
def test_owner_grant_cannot_be_inferred_or_widened(policy, key, value):
    policy[1]["alice"][key] = value
    with pytest.raises(profiles.ProfileError, match="consent_unavailable"):
        profiles.select("example/project", "security")


def test_queued_selection_does_not_silently_switch_accounts(policy):
    bound = profiles.select("example/project", "code")
    policy[0]["repositories"]["example/project"] = "bob"
    with pytest.raises(profiles.ProfileError, match="assignment_changed"):
        profiles.select("example/project", "code", reserved=bound)
    assert profiles.select("example/project", "code")["profile"] == "bob"


def test_duplicate_sessions_do_not_create_extra_quota(policy):
    policy[0]["profiles"]["bob"]["account_sha256"] = ACCOUNT
    with pytest.raises(profiles.ProfileError, match="duplicate_account"):
        profiles.select("example/project", "code")


def test_host_relocation_requires_reprovisioning(policy):
    policy[0]["host_sha256"] = "0" * 64
    with pytest.raises(profiles.ProfileError, match="registry_invalid"):
        profiles.select("example/project", "code")


def test_profile_switch_does_not_invalidate_review_policy(monkeypatch, policy):
    before = worker.policy_hash()
    policy[0]["repositories"]["example/project"] = "bob"
    assert worker.policy_hash() == before
    # All account selection is out of the repository-editable worker descriptor.
    with pytest.raises(worker.Incomplete, match="blocked_credentials"):
        worker.configured_profile(
            {"enabled": True, "credential_mode": "personal_profiles"},
            {"repository": "example/project", "lane": "code"},
            {"private": False},
        )


def test_completed_artifact_keeps_actual_owner_after_switch_or_revocation(
    policy, monkeypatch
):
    target = {
        "repository": "example/project",
        "pull_request": 1,
        "lane": "code",
        "base_sha": "b" * 40,
        "head_sha": "a" * 40,
    }
    monkeypatch.setenv("CODEX_REQUEST_RUN_ID", "3")
    monkeypatch.setenv("GITHUB_RUN_ID", "30")
    monkeypatch.setenv("GITHUB_WORKFLOW_SHA", "c" * 40)
    bound = profiles.select(target["repository"], target["lane"])
    output = "codex\nSynthetic completed review"
    record = worker.receipt_for(
        target,
        "completed",
        "verdict_produced",
        "sha256:" + "d" * 64,
        output=output,
        successful_turns=1,
        funding=bound,
        capacity={"observed_at": int(time.time())},
        provider_invocation_started=True,
    )
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as archive:
        archive.writestr("receipt.json", json.dumps(record))
        archive.writestr("review.txt", output)
    policy[1]["alice"]["revoked"] = True
    policy[0]["repositories"]["example/project"] = "bob"
    proof = worker.read_receipt(
        blob.getvalue(), target, worker.policy_hash(), run_id=30, workflow_sha="c" * 40
    )
    assert proof["funding_profile"] == bound
    assert proof["funding_profile"]["owner"] == "alice"
    assert proof["state"] == "completed"


def limits():
    return {
        "ordinaryUsageAllowed": True,
        "rateLimits": {
            "credits": {"hasCredits": False, "unlimited": False},
            "primary": {"usedPercent": 10, "resetsAt": int(time.time()) + 3600},
            "secondary": {"usedPercent": 20, "resetsAt": int(time.time()) + 604800},
        },
    }


def test_verified_included_capacity_is_required(policy):
    bound = profiles.select("example/project", "code")
    snapshot = profiles.capacity({"account": {"type": "chatgpt"}}, limits(), bound)
    assert snapshot["windows"]["primary"]["used_percent"] == 10


@pytest.mark.parametrize(
    "change,reason",
    [
        ("credits", "funding_unverified"),
        ("unlimited", "funding_unverified"),
        ("unknown_credits", "funding_unverified"),
        ("near_limit", "capacity_exhausted"),
        ("nan", "capacity_exhausted"),
        ("reset", "capacity_exhausted"),
        ("missing", "capacity_unknown"),
        ("permission", "capacity_unknown"),
        ("api_key", "account_unverified"),
    ],
)
def test_quota_and_funding_fail_closed(policy, change, reason):
    data = limits()
    account = {"account": {"type": "chatgpt"}}
    if change == "credits":
        data["rateLimits"]["credits"]["hasCredits"] = True
    if change == "unlimited":
        data["rateLimits"]["credits"]["unlimited"] = True
    if change == "unknown_credits":
        del data["rateLimits"]["credits"]
    if change == "near_limit":
        data["rateLimits"]["primary"]["usedPercent"] = 80
    if change == "nan":
        data["rateLimits"]["primary"]["usedPercent"] = float("nan")
    if change == "reset":
        data["rateLimits"]["secondary"]["resetsAt"] = 0
    if change == "missing":
        del data["rateLimits"]["secondary"]
    if change == "permission":
        data["ordinaryUsageAllowed"] = False
    if change == "api_key":
        account["account"]["type"] = "apiKey"
    with pytest.raises(profiles.ProfileError, match=reason):
        profiles.capacity(account, data, profiles.select("example/project", "code"))


def test_real_cross_process_lock_survives_profile_aliases_and_releases(
    tmp_path, monkeypatch, policy
):
    state = tmp_path / "state"
    home = state / "accounts" / ACCOUNT
    for path in (state, state / "accounts", home):
        path.mkdir(mode=0o700)
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 3600}).encode())
        .decode()
        .rstrip("=")
    )
    auth = {
        "auth_mode": "chatgpt",
        "tokens": {
            "account_id": "personal-account",
            "access_token": "public." + payload + ".fixture",
            "id_token": "fixture",
            "refresh_token": "fixture",
        },
    }
    (home / "auth.json").write_text(json.dumps(auth))
    (home / "auth.json").chmod(0o600)
    monkeypatch.setattr(profiles, "STATE", state)
    monkeypatch.setattr(
        profiles,
        "account_probe",
        lambda _: ({"account": {"type": "chatgpt"}}, limits()),
    )
    bound = profiles.select("example/project", "code")
    command = [
        sys.executable,
        "-c",
        "import fcntl,sys; f=open(sys.argv[1],'r+'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)",
        str(home / "review.lock"),
    ]
    with profiles.session(bound, "example/project", "code"):
        assert subprocess.run(command, capture_output=True).returncode != 0
        with pytest.raises(profiles.ProfileError, match="session_busy"):
            with profiles.session(bound, "example/project", "security", wait_seconds=0):
                pytest.fail("second lane must not acquire the shared account")
    assert subprocess.run(command, capture_output=True).returncode == 0
    with profiles.session(bound, "example/project", "code") as (result, _):
        assert result == auth
    auth["tokens"]["account_id"] = "wrong-account"
    (home / "auth.json").write_text(json.dumps(auth))
    with pytest.raises(profiles.ProfileError, match="account_mismatch"):
        with profiles.session(bound, "example/project", "code"):
            pytest.fail("wrong owner")


def test_models_preserve_production_routing():
    assert effort("security", ["docs/style.md"]) == "xhigh"
    assert effort("code", ["src/components/Button.tsx"]) == "medium"
    assert effort("code", ["auth/token.py"]) == "xhigh"
    assert effort("code", ["migrations/old.sql"]) == "xhigh"
    source = ROOT / "config/model-routing.source.yaml"
    if source.exists():
        import yaml

        data = yaml.safe_load(source.read_text())

        # The production routing source keeps advisor policy under agent_routing.
        def find(value):
            if isinstance(value, dict):
                if "advisor_policy" in value:
                    return value["advisor_policy"]["deterministic_triggers"][
                        "file_patterns"
                    ]
                for child in value.values():
                    result = find(child)
                    if result is not None:
                        return result

        assert tuple(find(data)) == ESCALATION


@pytest.mark.parametrize(
    "account,limit",
    [
        (None, None),
        ([], {}),
        ({}, []),
        ({"account": []}, {"rateLimits": []}),
        ({"account": {"type": "chatgpt"}}, {"rateLimits": True}),
    ],
)
def test_malformed_rpc_has_fixed_incomplete_category(policy, account, limit):
    with pytest.raises(profiles.ProfileError):
        profiles.capacity(account, limit, profiles.select("example/project", "code"))


def test_snapshot_account_must_match_reserved_owner(policy):
    data = limits()
    data["accountId"] = "wrong-account"
    with pytest.raises(profiles.ProfileError, match="account_mismatch"):
        profiles.capacity(
            {"account": {"type": "chatgpt"}},
            data,
            profiles.select("example/project", "code"),
        )


def test_probe_owns_and_reaps_native_child(tmp_path, monkeypatch):
    from types import SimpleNamespace

    # The protected system Python is a real native ELF. Its script simulates
    # only the RPC protocol, with no network, credentials or model turn.
    monkeypatch.setattr(profiles, "CODEX", "/usr/bin/python3")
    monkeypatch.setattr(
        profiles.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"codex-cli 0.163.0\n"),
    )
    (tmp_path / "app-server").write_text("""import json,os,sys,time
from pathlib import Path
Path("child.pid").write_text(str(os.getpid()))
for line in sys.stdin:
    request=json.loads(line)
    if "id" in request:
        result={"account":{"type":"chatgpt"}} if request["id"]==2 else {}
        print(json.dumps({"id":request["id"],"result":result}),flush=True)
time.sleep(30)
""")
    account, _ = profiles.account_probe(tmp_path)
    assert account["account"]["type"] == "chatgpt"
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "child.pid").read_text()), 0)


def test_protected_script_launcher_is_refused_before_auth(tmp_path, monkeypatch):
    path = Path("/usr/bin/which")
    if not path.exists() or path.read_bytes()[:2] != b"#!":
        pytest.skip("requires a protected system script for the negative control")
    monkeypatch.setattr(profiles, "CODEX", str(path))
    with pytest.raises(profiles.ProfileError, match="cli_native_required"):
        profiles.account_probe(tmp_path)


def test_parent_death_stops_the_native_auth_writer(tmp_path):
    (tmp_path / "app-server").write_text("""import os,time
from pathlib import Path
Path("child.pid").write_text(str(os.getpid()))
time.sleep(30)
""")
    code = """import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
import codex_review_profiles as p
p.CODEX="/usr/bin/python3"
p.subprocess.run=lambda *a,**k:SimpleNamespace(returncode=0,stdout=b"codex-cli 0.163.0\\n")
p.account_probe(Path(sys.argv[2]))
"""
    with subprocess.Popen(
        [sys.executable, "-c", code, str(ROOT / "scripts/ci"), str(tmp_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) as controller:
        try:
            deadline = time.monotonic() + 5
            while not (tmp_path / "child.pid").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert (tmp_path / "child.pid").exists()
            child = int((tmp_path / "child.pid").read_text())
            controller.kill()
            controller.wait(timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                status = Path(f"/proc/{child}/status")
                if not status.exists() or "State:\tZ" in status.read_text():
                    break
                time.sleep(0.02)
            else:
                pytest.fail("native refresh writer outlived its killed controller")
        finally:
            if controller.poll() is None:
                controller.kill()
                controller.wait(timeout=5)
