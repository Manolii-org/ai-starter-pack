import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("devin_admission", Path(__file__).parents[1] / "devin_admission.py")
admission = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(admission)


def test_all_clear_never_dispatches_or_creates_state(tmp_path):
    db = tmp_path / "state.sqlite3"
    assert admission.admit(db, "health", "episode", False, {}, lambda p: pytest.fail("dispatch")) == {"decision": "noop"}
    assert not db.exists()


def test_one_actionable_one_primary_across_duplicate_and_changed_payload(tmp_path):
    db, writes = tmp_path / "state.sqlite3", []

    def dispatch(payload):
        writes.append(payload)
        return "primary-session"

    assert admission.admit(db, "health", "episode", True, {}, dispatch)["decision"] == "dispatched"
    result = admission.admit(db, "health", "episode", True, {"repeated_failure": 2}, dispatch)
    assert result == {"decision": "duplicate", "state": "dispatched", "primary_ref": "primary-session"}
    assert len(writes) == 1


def test_ambiguous_failure_remains_reserved(tmp_path):
    db = tmp_path / "state.sqlite3"

    def dispatch(payload):
        raise TimeoutError("response lost")

    with pytest.raises(TimeoutError):
        admission.admit(db, "incident", "first-episode", True, {}, dispatch)
    result = admission.admit(db, "incident", "first-episode", True, {}, lambda p: pytest.fail("retry"))
    assert result == {"decision": "duplicate", "state": "reserved", "primary_ref": None}


def test_dry_run_no_side_effects(tmp_path):
    db = tmp_path / "state.sqlite3"
    assert admission.admit(db, "health", "episode", True, {}, lambda p: pytest.fail("dispatch"), dry_run=True)["decision"] == "would_dispatch"
    assert not db.exists()


def test_concurrent_delivery(tmp_path):
    db, writes = tmp_path / "state.sqlite3", []

    def deliver(_):
        return admission.admit(db, "health", "episode", True, {}, lambda p: writes.append(p) or "primary")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(deliver, range(10)))
    assert len(writes) == 1
    assert sum(r["decision"] == "dispatched" for r in results) == 1


@pytest.mark.parametrize("value", [None, "false", 1, []])
def test_unknown_signal_fails_closed(tmp_path, value):
    with pytest.raises(ValueError):
        admission.admit(tmp_path / "state", "health", "episode", value, {}, lambda p: pytest.fail("dispatch"))


@pytest.mark.parametrize("key", [None, True, 1, 1.0, float("nan"), [], {}, "", "  "])
@pytest.mark.parametrize("position", [0, 1])
def test_invalid_dedup_keys_never_create_state(tmp_path, key, position):
    keys = ["health", "episode"]
    keys[position] = key
    db = tmp_path / "state"
    with pytest.raises(ValueError):
        admission.admit(db, *keys, True, {}, lambda p: pytest.fail("dispatch"))
    assert not db.exists()


@pytest.mark.parametrize("database", ["", ":memory:", "file::memory:", "file:state?mode=memory"])
def test_volatile_or_uri_database_rejected(database):
    with pytest.raises(ValueError):
        admission.admit(database, "health", "episode", True, {}, lambda p: pytest.fail("dispatch"))
