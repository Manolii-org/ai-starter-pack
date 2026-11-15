"""Producer contract for subscription broker `automation.inference/v2` receipts."""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from ci import codex_automation_receipts as receipts
from test_codex_subscription_text import (
    Response,
    auth,
    broker,
    client,
    completion,
)

COORDINATES = {
    "repository": "example/automation", "run_id": 41, "run_attempt": 2,
    "base_sha": "b" * 40, "head_sha": "a" * 40, "workflow_sha": "c" * 40,
    "role": "assessment", "data_sensitivity": "internal",
    "correlation_id": "example/automation#7@" + "a" * 40,
}


def completed(**changes):
    value = {"model": broker.REVIEW_MODEL, "model_verification": "provider_reported",
             "usage": {"input_tokens": 31, "output_tokens": 8, "cached_input_tokens": 12},
             "input_chars": 120, "observed_at": "2026-11-14T03:30:00Z"}
    value.update(changes)
    return value


def test_actual_broker_records_one_receipt_per_completion_with_actual_input_size(tmp_path, monkeypatch):
    path = tmp_path / "model.sock"
    monkeypatch.setattr(client, "SOCKET", str(path))
    with broker.serve(path, auth(), transport=lambda *a, **k: Response(completion()),
                      text_only=True, expected_effort="medium", max_requests=2, max_seconds=5) as host:
        client.invoke("SYS", "USER-TEXT", role="assessment", data_sensitivity="internal", seconds=3)
        client.invoke("S", "U", role="assessment", data_sensitivity="internal", seconds=3)
        audit = host.audit()
    rows = audit["text_completions"]
    assert len(rows) == 2 and all("text" not in row for row in rows)
    assert rows[0]["input_chars"] > rows[1]["input_chars"] > 0
    emitted = receipts.build(rows, **COORDINATES)
    assert [r["attempt_id"] for r in emitted] == ["41.2:assessment:1", "41.2:assessment:2"]
    assert all(r["subscription_eligible"] and r["cost_usd"] is None for r in emitted)
    assert all(r["model"] == broker.REVIEW_MODEL for r in emitted)


def test_rows_are_complete_nonsecret_and_round_trip_as_jsonl():
    rows = receipts.build([completed()], **COORDINATES)
    text = receipts.dumps(rows)
    assert set(json.loads(text)) == receipts.FIELDS
    assert "text" not in json.loads(text) and text.endswith("\n")


@pytest.mark.parametrize("change", [
    {"model_verification": "selected"},
    {"usage": {"input_tokens": 1, "output_tokens": 1}},
    {"usage": {"input_tokens": 1, "output_tokens": 1, "cached_input_tokens": 2}},
    {"usage": {"input_tokens": True, "output_tokens": 1, "cached_input_tokens": 0}},
    {"observed_at": "2026-11-14T03:30:00"},
    {"observed_at": None},
    {"model": ""},
])
def test_unverified_or_malformed_completions_never_become_receipts(change):
    with pytest.raises(receipts.ReceiptError):
        receipts.build([completed(**change)], **COORDINATES)


def test_unknown_or_oversized_input_is_recorded_ineligible_not_guessed():
    rows = receipts.build([completed(input_chars=None), completed(input_chars=180001)], **COORDINATES)
    assert [r["subscription_eligible"] for r in rows] == [False, False]
    assert rows[0]["input_chars"] is None


@pytest.mark.parametrize(("field", "bad"), [
    ("role", "implementation"), ("data_sensitivity", "restricted"), ("run_id", 0),
    ("run_attempt", True), ("head_sha", "A" * 40), ("repository", "no-slash"),
    ("correlation_id", "bad\nid"),
])
def test_coordinates_outside_the_reader_contract_are_refused(field, bad):
    with pytest.raises(receipts.ReceiptError):
        receipts.build([completed()], **{**COORDINATES, field: bad})


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(cost_usd=0), lambda r: r.update(fallback_reason="x"),
    lambda r: r.update(transport="subscription_cli"), lambda r: r.pop("usage"),
    lambda r: r.update(extra=1), lambda r: r.update(subscription_eligible=1),
])
def test_validator_matches_owning_reader_refusals(mutate):
    row = copy.deepcopy(receipts.build([completed()], **COORDINATES)[0])
    mutate(row)
    with pytest.raises(receipts.ReceiptError):
        receipts.validate(row)


def test_missing_completion_list_is_not_an_empty_success():
    with pytest.raises(receipts.ReceiptError):
        receipts.build(None, **COORDINATES)
