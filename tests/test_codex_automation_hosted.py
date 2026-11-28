"""Hosted mode: consent from the reviewed policy, private login copy, one writer."""

import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "ci"))
import codex_automation_hosted as hosted  # noqa: E402
import codex_review_profiles as profiles  # noqa: E402

REPO = "example/project"
ACCOUNT = "acct-fixture"
ACCOUNT_SHA = hashlib.sha256(ACCOUNT.encode()).hexdigest()
BACKUP_ACCOUNT = "acct-backup"
BACKUP_SHA = hashlib.sha256(BACKUP_ACCOUNT.encode()).hexdigest()


def jwt(exp):
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return "h." + body + ".s"


def login(seed="a", exp=None):
    exp = exp or int(time.time()) + 86400
    return {"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
            "tokens": {"account_id": ACCOUNT, "access_token": jwt(exp), "refresh_token": "r" + seed,
                       "id_token": "i" + seed}}


def policy(**consent):
    value = {"schema": hosted.SCHEMA, "owner": "Owner", "account_sha256": ACCOUNT_SHA, "revoked": False,
             "expires_at": int(time.time()) + 3600, "roles": ["assessment", "judge"], "repositories": [REPO],
             "purchased_credits_disabled": True, "automatic_purchases_disabled": True}
    value.update(consent)
    return {"consent": value,
            "credential": {"doppler_project": "master", "doppler_config": "prd_codex",
                           "secret": "CODEX_SUBSCRIPTION_AUTH_JSON"}}


def backup_policy():
    value = policy()
    backup = policy(owner="Backup Owner", account_sha256=BACKUP_SHA)
    backup["credential"]["secret"] = "CODEX_SUBSCRIPTION_AUTH_JSON_BACKUP"
    backup["credential_mode"] = "access_only"
    value["backup"] = backup
    return value


@pytest.fixture
def controller(monkeypatch):
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", REPO + "/.github/workflows/codex-automation-worker.yml@refs/heads/main")


def test_owner_consent_in_the_default_branch_policy_admits_the_role(controller):
    assignment = hosted.select(REPO, "assessment", policy())
    assert assignment["mode"] == "hosted" and assignment["account_sha256"] == ACCOUNT_SHA
    assert assignment["credential"]["doppler_config"] == "prd_codex"


@pytest.mark.parametrize("change", [
    {"expires_at": 1}, {"revoked": True}, {"roles": ["judge"]}, {"repositories": ["other/repo"]},
    {"purchased_credits_disabled": False}, {"automatic_purchases_disabled": None},
    {"account_sha256": "x"}, {"schema": "other"}, {"owner": " "}, {"credit_balance_allowed": "yes"},
])
def test_missing_or_weakened_consent_is_refused(controller, change):
    with pytest.raises(profiles.ProfileError, match="consent_unavailable"):
        hosted.select(REPO, "assessment", policy(**change))


def test_credit_balance_admission_is_carried_only_when_the_owner_grants_it(controller):
    assert "credit_balance_allowed" not in hosted.select(REPO, "assessment", policy())
    assert "credit_balance_allowed" not in hosted.select(REPO, "assessment", policy(credit_balance_allowed=False))
    granted = hosted.select(REPO, "assessment", policy(credit_balance_allowed=True))
    assert granted["credit_balance_allowed"] is True
    # The grant is part of the bound assignment, not a side channel.
    assert granted["assignment_sha256"] != hosted.select(REPO, "assessment", policy())["assignment_sha256"]


def test_only_the_default_branch_worker_controller_is_admitted(monkeypatch):
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", REPO + "/.github/workflows/other.yml@refs/heads/main")
    with pytest.raises(profiles.ProfileError, match="host_access_unverified"):
        hosted.select(REPO, "assessment", policy())


@pytest.mark.parametrize("credential", [
    None, {"doppler_project": "master"},
    {"doppler_project": "../x", "doppler_config": "prd", "secret": "A"},
    {"doppler_project": "m", "doppler_config": "prd", "secret": "lower"},
])
def test_credential_coordinates_are_validated(controller, credential):
    value = policy()
    value["credential"] = credential
    with pytest.raises(profiles.ProfileError, match="coordinates_invalid"):
        hosted.select(REPO, "assessment", value)


class FakeDoppler:
    def __init__(self, stored):
        self.stored, self.writes = json.dumps(stored), []

    def __call__(self, token, url, data=None):
        assert token == "dp.st.fixture"
        if data is None:
            return {"value": {"raw": self.stored}}
        assert data["project"] == "master" and data["config"] == "prd_codex"
        self.stored = data["secrets"]["CODEX_SUBSCRIPTION_AUTH_JSON"]
        self.writes.append(self.stored)
        return {"success": True}


@pytest.fixture
def two_accounts(monkeypatch, tmp_path, controller):
    value = backup_policy()
    home = tmp_path / "home"
    secondary = login("backup")
    secondary["tokens"].update(account_id=BACKUP_ACCOUNT, refresh_token="")
    logins = {
        value["credential"]["secret"]: login(),
        value["backup"]["credential"]["secret"]: secondary,
    }
    reads, probes = [], []

    def request(token, url, data=None):
        assert token == "dp.st.fixture" and data is None
        name = url.rsplit("name=", 1)[1]
        reads.append(name)
        if name not in logins:
            raise profiles.ProfileError("hosted_credential_unavailable")
        return {"value": {"raw": json.dumps(logins[name])}}

    def probe(path, *, refresh):
        account = json.loads((path / "auth.json").read_text())["tokens"]["account_id"]
        probes.append(account)
        if account == BACKUP_ACCOUNT:
            assert refresh is False and path == home / "backup"
        return {}, {}

    monkeypatch.setattr(hosted, "_request", request)
    monkeypatch.setattr(profiles, "account_probe", probe)
    monkeypatch.setattr(profiles, "capacity", lambda *args: {"windows": {}})
    hosted.configure(value, home, "dp.st.fixture")
    return value, logins, reads, probes


def test_preferred_account_is_used_without_reading_backup(two_accounts):
    value, _, reads, probes = two_accounts
    with hosted.session(hosted.select(REPO, "assessment", value), REPO, "assessment") as (auth, _):
        assert auth["tokens"]["account_id"] == ACCOUNT
    assert hosted.account_selection() == {"slot": "primary"}
    assert reads == [value["credential"]["secret"]]
    assert probes == [ACCOUNT]


@pytest.mark.parametrize("reason", ["profile_capacity_exhausted", "profile_probe_unavailable"])
def test_backup_stays_selected_across_the_assessment_and_judge(two_accounts, monkeypatch, reason):
    value, _, _, probes = two_accounts
    probe = profiles.account_probe

    def refuse_primary(path, *, refresh):
        result = probe(path, refresh=refresh)
        if path.name == "home":
            raise profiles.ProfileError(reason)
        return result

    monkeypatch.setattr(profiles, "account_probe", refuse_primary)
    for role in ("assessment", "judge"):
        with hosted.session(hosted.select(REPO, role, value), REPO, role) as (auth, _):
            assert auth["tokens"]["account_id"] == BACKUP_ACCOUNT
            assert not auth["tokens"]["refresh_token"]
    assert probes == [ACCOUNT, BACKUP_ACCOUNT, BACKUP_ACCOUNT]
    assert hosted.account_selection() == {"slot": "backup", "primary_reason": reason}


def test_credential_mismatch_does_not_activate_backup(two_accounts):
    value, logins, reads, _ = two_accounts
    logins[value["credential"]["secret"]]["tokens"]["account_id"] = "unapproved"
    with pytest.raises(profiles.ProfileError, match="profile_account_mismatch"):
        with hosted.session(hosted.select(REPO, "assessment", value), REPO, "assessment"):
            pytest.fail("an unapproved account cannot run")
    assert reads == [value["credential"]["secret"]]


def test_no_fallback_after_primary_session_is_admitted(two_accounts, monkeypatch):
    value, _, reads, _ = two_accounts
    with hosted.session(hosted.select(REPO, "assessment", value), REPO, "assessment"):
        pass

    def fail(*args, **kwargs):
        raise profiles.ProfileError("profile_capacity_exhausted")

    monkeypatch.setattr(profiles, "account_probe", fail)
    with pytest.raises(profiles.ProfileError, match="profile_capacity_exhausted"):
        with hosted.session(hosted.select(REPO, "judge", value), REPO, "judge"):
            pytest.fail("the judge cannot switch accounts")
    assert reads == [value["credential"]["secret"]]


def test_rejects_backup_with_refresh_token(two_accounts):
    value, logins, _, probes = two_accounts
    del logins[value["credential"]["secret"]]
    logins[value["backup"]["credential"]["secret"]]["tokens"]["refresh_token"] = "forbidden"
    with pytest.raises(profiles.ProfileError, match="profile_backup_refresh_material"):
        with hosted.session(hosted.select(REPO, "assessment", value), REPO, "assessment"):
            pytest.fail("a backup cannot hold refresh material")
    assert not probes


@pytest.mark.parametrize("conflict", ["account", "secret", "config"])
def test_backup_must_be_a_different_account_and_secret_at_the_same_scope(controller, conflict):
    value = backup_policy()
    backup = value["backup"]
    if conflict == "account":
        backup["consent"]["account_sha256"] = ACCOUNT_SHA
    if conflict == "secret":
        backup["credential"]["secret"] = value["credential"]["secret"]
    if conflict == "config":
        backup["credential"]["doppler_config"] = "different"
    assignment = hosted.select(REPO, "assessment", value)
    assert "backup" not in assignment and assignment["backup_unavailable"] == "profile_backup_not_distinct"


def test_revoked_backup_does_not_interrupt_primary(two_accounts):
    value, _, reads, probes = two_accounts
    value["backup"]["consent"]["revoked"] = True
    with hosted.session(hosted.select(REPO, "assessment", value), REPO, "assessment"):
        pass
    assert hosted.account_selection() == {"slot": "primary", "backup_unavailable": "profile_consent_unavailable"}
    assert reads == [value["credential"]["secret"]] and probes == [ACCOUNT]


def test_backup_coordinates_are_bound_before_primary_attempt(two_accounts):
    value, logins, reads, _ = two_accounts
    assignment = hosted.select(REPO, "assessment", value)
    value["backup"]["credential"]["secret"] = "UPDATED_BACKUP"
    del logins[value["credential"]["secret"]]
    with pytest.raises(profiles.ProfileError, match="profile_assignment_changed"):
        with hosted.session(assignment, REPO, "assessment"):
            pytest.fail("an updated backup secret needs a new assignment")
    assert reads == [value["credential"]["secret"]]


def test_login_is_materialized_privately_and_unchanged_logins_are_not_written(monkeypatch, tmp_path, controller):
    fake = FakeDoppler(login())
    monkeypatch.setattr(hosted, "_request", fake)
    home = tmp_path / "home"
    hosted.configure(policy(), home, "dp.st.fixture")
    assert oct((home / "auth.json").stat().st_mode & 0o777) == "0o600"
    assert oct(home.stat().st_mode & 0o777) == "0o700"
    assert hosted.persist() == "unchanged" and fake.writes == []


def test_a_rotated_login_is_written_back_once_and_read_back(monkeypatch, tmp_path, controller):
    fake = FakeDoppler(login("a"))
    monkeypatch.setattr(hosted, "_request", fake)
    home = tmp_path / "home"
    hosted.configure(policy(), home, "dp.st.fixture")
    (home / "auth.json").write_text(json.dumps(login("b")))
    assert hosted.persist() == "persisted" and len(fake.writes) == 1
    assert json.loads(fake.stored)["tokens"]["refresh_token"] == "rb"
    assert hosted.persist() == "unchanged" and len(fake.writes) == 1


def test_never_restores_over_a_login_another_writer_advanced(monkeypatch, tmp_path, controller):
    fake = FakeDoppler(login("a"))
    monkeypatch.setattr(hosted, "_request", fake)
    home = tmp_path / "home"
    hosted.configure(policy(), home, "dp.st.fixture")
    fake.stored = json.dumps(login("other"))
    (home / "auth.json").write_text(json.dumps(login("b")))
    assert hosted.persist() == "superseded" and fake.writes == []
    assert json.loads(fake.stored)["tokens"]["refresh_token"] == "rother"


def test_session_probes_the_account_then_persists_the_refresh(monkeypatch, tmp_path, controller):
    fake = FakeDoppler(login("a"))
    monkeypatch.setattr(hosted, "_request", fake)
    home = tmp_path / "home"
    value = policy()
    hosted.configure(value, home, "dp.st.fixture")
    assignment = hosted.select(REPO, "assessment", value)

    def probe(path, refresh=True):
        assert path == home
        (home / "auth.json").write_text(json.dumps(login("b")))
        (home / "auth.json").chmod(0o600)
        return {"account": {"type": "chatgpt"}}, {"rateLimits": {}}
    monkeypatch.setattr(profiles, "account_probe", probe)
    monkeypatch.setattr(profiles, "capacity", lambda a, l, s: {"windows": {}})
    with hosted.session(assignment, REPO, "assessment") as (auth, snapshot):
        assert auth["tokens"]["refresh_token"] == "rb" and snapshot["credential"] == "persisted"
    with pytest.raises(profiles.ProfileError, match="assignment_changed"):
        with hosted.session(assignment, REPO, "judge"):
            pass


def test_access_only_worker_reloads_current_login_without_a_refresh_token(monkeypatch, tmp_path, controller):
    fake = FakeDoppler(login("old"))
    monkeypatch.setattr(hosted, "_request", fake)
    value = {**policy(), "credential_mode": "access_only"}
    home = tmp_path / "home"
    hosted.configure(value, home, "dp.st.fixture")
    assignment = hosted.select(REPO, "assessment", value)
    probes = []

    def probe(path, *, refresh):
        assert refresh is False
        tokens = json.loads((path / "auth.json").read_text())["tokens"]
        assert tokens["refresh_token"] == ""
        probes.append(tokens["access_token"])
        return {}, {}

    monkeypatch.setattr(profiles, "account_probe", probe)
    monkeypatch.setattr(profiles, "capacity", lambda *_: {"windows": {}})
    fake.stored = json.dumps(login("expiring", exp=int(time.time()) + 60))
    with pytest.raises(profiles.ProfileError, match="profile_session_expiry_unverified"):
        with hosted.session(assignment, REPO, "assessment"):
            pytest.fail("short-lived login reached the broker")
    assert probes == []

    renewed = login("renewed")
    fake.stored = json.dumps(renewed)
    with hosted.session(assignment, REPO, "assessment") as (auth, snapshot):
        assert auth["tokens"]["refresh_token"] == ""
        assert snapshot["credential"] == "access_only"
    assert probes == [renewed["tokens"]["access_token"]]
    assert hosted.persist() == "access_only" and fake.writes == []
    assert hosted.select(REPO, "assessment", policy())["assignment_sha256"] != assignment["assignment_sha256"]


def test_access_only_worker_rejects_insufficient_lifetime_at_configuration(monkeypatch, tmp_path):
    fake = FakeDoppler(login(exp=int(time.time()) + 1600))
    monkeypatch.setattr(hosted, "_request", fake)
    value = {**policy(), "credential_mode": "access_only", "max_seconds": 1800}
    with pytest.raises(profiles.ProfileError, match="profile_session_expiry_unverified"):
        hosted.configure(value, tmp_path / "home", "dp.st.fixture")
    assert fake.writes == []


def test_unrecognized_credential_mode_is_refused_before_reading_doppler(monkeypatch, tmp_path):
    monkeypatch.setattr(hosted, "_request", lambda *a, **k: pytest.fail("bad policy must not reach Doppler"))
    with pytest.raises(profiles.ProfileError, match="hosted_credential_mode_invalid"):
        hosted.configure({**policy(), "credential_mode": "rotate"}, tmp_path / "home", "dp.st.fixture")


def test_a_login_for_another_account_is_refused(monkeypatch, tmp_path, controller):
    other = login()
    other["tokens"]["account_id"] = "someone-else"
    monkeypatch.setattr(hosted, "_request", FakeDoppler(other))
    home = tmp_path / "home"
    value = policy()
    hosted.configure(value, home, "dp.st.fixture")
    with pytest.raises(profiles.ProfileError, match="account_mismatch"):
        with hosted.session(hosted.select(REPO, "assessment", value), REPO, "assessment"):
            pass


def test_short_lived_access_tokens_are_refused():
    with pytest.raises(profiles.ProfileError, match="expiry_unverified"):
        profiles.require_session_lifetime(login(exp=int(time.time()) + 60))
    profiles.require_session_lifetime(login())


def test_missing_token_holds_before_any_read(monkeypatch, tmp_path):
    monkeypatch.setattr(hosted, "_request", lambda *a, **k: pytest.fail("no read without a token"))
    with pytest.raises(profiles.ProfileError, match="credential_unavailable"):
        hosted.configure(policy(), tmp_path / "home", "")


def test_hosted_mode_is_explicit(monkeypatch):
    monkeypatch.delenv("CODEX_AUTOMATION_HOSTED", raising=False)
    assert hosted.enabled() is False
    monkeypatch.setenv("CODEX_AUTOMATION_HOSTED", "1")
    assert hosted.enabled() is True
    assert os.environ.get("CODEX_AUTOMATION_DOPPLER_TOKEN") is None


def test_worker_takes_consent_from_the_consumer_policy_in_hosted_mode(monkeypatch, tmp_path, controller):
    import codex_automation_worker as worker
    base = json.loads(worker.CONFIG.read_text())
    consumer = tmp_path / "codex-automation.json"
    consumer.write_text(json.dumps({**base, **policy()}))
    monkeypatch.setenv("CODEX_AUTOMATION_HOSTED", "1")
    monkeypatch.setenv("CODEX_AUTOMATION_POLICY", str(consumer))
    monkeypatch.setattr(profiles, "select_automation", lambda *a, **k: pytest.fail("no host registry in hosted mode"))
    assignments = worker.consent({"repository": REPO}, ("assessment", "judge"))
    assert {a["mode"] for a in assignments.values()} == {"hosted"}
    monkeypatch.setenv("CODEX_AUTOMATION_POLICY", "relative.json")
    with pytest.raises(worker.Incomplete, match="controller_policy_invalid"):
        worker.policy()
    monkeypatch.delenv("CODEX_AUTOMATION_HOSTED")
    assert worker._policy_path() == worker.CONFIG


def test_requests_only_go_to_the_fixed_doppler_endpoint():
    with pytest.raises(profiles.ProfileError, match="credential_unavailable"):
        hosted._request("dp.st.fixture", "file:///etc/passwd")


def test_a_read_only_token_runs_without_writing_back(monkeypatch, tmp_path, controller):
    fake = FakeDoppler(login("a"))

    def read_only(token, url, data=None):
        if data is not None:
            raise profiles.ProfileError("hosted_credential_unavailable")
        return fake(token, url)
    monkeypatch.setattr(hosted, "_request", read_only)
    home = tmp_path / "home"
    hosted.configure(policy(), home, "dp.st.fixture")
    (home / "auth.json").write_text(json.dumps(login("b")))
    assert hosted.persist() == "writeback_unavailable"
    assert json.loads(fake.stored)["tokens"]["refresh_token"] == "ra"


def test_a_rotated_login_is_saved_even_when_a_later_check_refuses_the_run(monkeypatch, tmp_path, controller):
    fake = FakeDoppler(login("a", exp=int(time.time()) + 60))
    monkeypatch.setattr(hosted, "_request", fake)
    home = tmp_path / "home"
    value = policy()
    hosted.configure(value, home, "dp.st.fixture")
    seen = {}

    def probe(path, refresh=True):
        seen["refresh"] = refresh
        (home / "auth.json").write_text(json.dumps(login("b")))
        return {}, {}

    def exhausted(*a):
        raise profiles.ProfileError("profile_capacity_exhausted")
    monkeypatch.setattr(profiles, "account_probe", probe)
    monkeypatch.setattr(profiles, "capacity", exhausted)
    with pytest.raises(profiles.ProfileError, match="capacity_exhausted"):
        with hosted.session(hosted.select(REPO, "assessment", value), REPO, "assessment"):
            pass
    assert seen["refresh"] is True and len(fake.writes) == 1
    assert json.loads(fake.stored)["tokens"]["refresh_token"] == "rb"


def test_a_fresh_login_is_not_refreshed(monkeypatch, tmp_path, controller):
    monkeypatch.setattr(hosted, "_request", FakeDoppler(login("a")))
    home = tmp_path / "home"
    value = policy()
    hosted.configure(value, home, "dp.st.fixture")
    seen = {}
    monkeypatch.setattr(profiles, "account_probe", lambda path, refresh=True: seen.update(refresh=refresh) or ({}, {}))
    monkeypatch.setattr(profiles, "capacity", lambda a, l, s: {"windows": {}})
    with hosted.session(hosted.select(REPO, "assessment", value), REPO, "assessment") as (_, snapshot):
        assert snapshot["credential"] == "unchanged"
    assert seen["refresh"] is False


def test_consent_is_rechecked_before_each_session(monkeypatch, tmp_path, controller):
    monkeypatch.setattr(hosted, "_request", FakeDoppler(login("a")))
    home = tmp_path / "home"
    value = policy()
    hosted.configure(value, home, "dp.st.fixture")
    assignment = hosted.select(REPO, "assessment", value)
    monkeypatch.setattr(profiles, "account_probe", lambda *a, **k: pytest.fail("no probe after consent lapsed"))
    value["consent"]["expires_at"] = int(time.time()) - 1
    with pytest.raises(profiles.ProfileError, match="consent_unavailable"):
        with hosted.session(assignment, REPO, "assessment"):
            pass
    value["consent"]["expires_at"] = int(time.time()) + 7200
    with pytest.raises(profiles.ProfileError, match="assignment_changed"):
        with hosted.session(assignment, REPO, "assessment"):
            pass


def test_consent_that_lapses_during_the_probe_is_refused_before_use(monkeypatch, tmp_path, controller):
    monkeypatch.setattr(hosted, "_request", FakeDoppler(login("a")))
    home = tmp_path / "home"
    value = policy()
    hosted.configure(value, home, "dp.st.fixture")
    assignment = hosted.select(REPO, "assessment", value)

    def probe(path, refresh=True):
        value["consent"]["expires_at"] = int(time.time()) - 1
        return {}, {}
    monkeypatch.setattr(profiles, "account_probe", probe)
    monkeypatch.setattr(profiles, "capacity", lambda a, l, s: {"windows": {}})
    with pytest.raises(profiles.ProfileError, match="consent_unavailable"):
        with hosted.session(assignment, REPO, "assessment"):
            pytest.fail("login handed over after consent lapsed")


def test_hosted_worker_can_post_its_pull_request_comment():
    import yaml
    flow = yaml.safe_load((Path(__file__).resolve().parents[1]
                           / ".github/workflows/codex-automation-worker-reusable.yml").read_text())
    # Commenting on a pull request needs pull-requests: write; issues: write alone is refused (403).
    assert flow["jobs"]["execute"]["permissions"]["pull-requests"] == "write"
    assert flow["jobs"]["publish_fix"]["permissions"]["pull-requests"] == "write"


def test_a_failed_guest_image_build_reports_its_cause(capsys):
    import subprocess
    import codex_automation_worker as worker
    worker._report_build_failure(subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"step 3: apt failed\n"))
    assert "apt failed" in capsys.readouterr().err


def test_apt_can_read_the_build_ca_secret():
    """apt fetches as the unprivileged _apt user; a root-only secret mount breaks every download."""
    root = Path(__file__).resolve().parents[1] / "scripts" / "ci"
    for name in ("codex-automation-runtime", "codex-review-runtime"):
        text = (root / name / "Dockerfile").read_text()
        # Fold Dockerfile line-continuations so each RUN instruction is one string.
        runs, pending = [], []
        for raw in text.splitlines():
            pending.append(raw)
            if raw.rstrip().endswith("\\"):
                continue
            runs.append(" ".join(pending))
            pending = []
        checked = 0
        for run in runs:
            if not (run.startswith("RUN --mount=type=secret") and "apt-get" in run):
                continue
            mounts = [tok for tok in run.split() if tok.startswith("--mount=")]
            proxy_ca = [m for m in mounts if "id=proxy_ca" in m]
            assert proxy_ca, f"{name}: apt RUN must mount proxy_ca"
            assert "mode=0444" in proxy_ca[0], (
                f"{name}: proxy_ca mount in every apt RUN must be mode=0444"
            )
            checked += 1
        assert checked, f"{name}: no apt RUN with proxy_ca mount was checked"


def test_hosted_install_pins_a_root_owned_cli_path():
    """account_probe refuses a CLI whose ancestors are not root-owned and write-protected."""
    import yaml
    flow = yaml.safe_load((Path(__file__).resolve().parents[1]
                           / ".github/workflows/codex-automation-worker-reusable.yml").read_text())
    steps = {s.get("name"): s for s in flow["jobs"]["execute"]["steps"]}
    install = steps["Install the pinned native Codex CLI (root-owned)"]
    run = install["run"]
    gate = "steps.prepare.outputs.needed == 'true'"
    guest = steps["Run consented, custody-fenced guest passes"]
    assert install["if"] == guest["if"] == gate
    order = list(steps.values())
    assert order.index(steps["Admit exact request"]) < order.index(install) < order.index(guest)
    assert "sudo chown root:root /opt" in run and "sudo chmod go-w / /opt" in run
    assert run.index("chmod go-w") < run.index("/opt/codex-review/bin/codex --version")
