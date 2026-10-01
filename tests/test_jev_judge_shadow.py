"""Observe-only Jev judge shadow: activation gates, fail-open, payload-free receipts."""

from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts/jev_judge_shadow.py"
REUSABLE = REPO_ROOT / ".github/workflows/pr-assessment-reusable.yml"

pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="pack-only runner")

_spec = importlib.util.spec_from_file_location("jev_judge_shadow", SCRIPT)
shadow = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shadow)

SHA = "abcdef1234567890"
SECRET_MESSAGE = "unique-finding-text-must-not-leak"
KEY = "test-key-value-not-real"


def _env(**over: str) -> dict[str, str]:
    env = {
        "JEV_ENABLED_JUDGE_FINDING_SHADOW": "1",
        "JEV_SHADOW_ENTITY": "impaktful",
        "TYPESAFE_API_KEY": KEY,
        "GITHUB_REPOSITORY_OWNER": "Impaktful-Platform",
        "GITHUB_REPOSITORY": "Impaktful-Platform/impaktful_3.0",
    }
    env.update(over)
    return env


def _workspace(tmp_path: Path, decisions: dict[str, str]) -> dict[str, Path]:
    cands = tmp_path / "cands"
    log = tmp_path / "log"
    cands.mkdir()
    log.mkdir()
    (tmp_path / "src.py").write_text("x = 1\npassword = 'hunter2hunter2'\n")
    findings = [
        {
            "finding_id": fid,
            "file": "src.py",
            "line": 2,
            "severity": "WARNING",
            "message": SECRET_MESSAGE,
            "fix": "do x",
        }
        for fid in decisions
    ]
    (cands / "skill.json").write_text(
        json.dumps({"source": "skill", "findings": findings})
    )
    (cands / "manifest.json").write_text("{}")
    (log / f"7-{SHA[:8]}.jsonl").write_text(
        "\n".join(
            json.dumps({"finding_id": f, "decision": d}) for f, d in decisions.items()
        )
    )
    return {"candidates_dir": cands, "judge_log_dir": log, "workspace": tmp_path}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _opener(
    nouls: dict[str, float], model: str = shadow.MODEL, sink: list | None = None
):
    def opener(request, timeout):
        if sink is not None:
            sink.append(request)
        body = {
            "model": model,
            "answers": {q: {"type": "noul", "noul": v} for q, v in nouls.items()},
        }
        return _Resp(json.dumps(body).encode())

    return opener


ALL_HIGH = dict.fromkeys(shadow.judge_shadow_questions(), 0.9)


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"JEV_ENABLED_JUDGE_FINDING_SHADOW": "0"}, "flag_off"),
        ({"JEV_ENABLED_JUDGE_FINDING_SHADOW": "true"}, "flag_off"),
        ({"JEV_ENABLED_JUDGE_FINDING_SHADOW": ""}, "flag_off"),
        ({"GITHUB_REPOSITORY_OWNER": "CPDcheck"}, "owner_denied"),
        ({"GITHUB_REPOSITORY_OWNER": "cpdcheck"}, "owner_denied"),
        ({"CLIENT_AI_POLICY": "1"}, "client_ai_policy"),
        ({"JEV_SHADOW_ENTITY": ""}, "entity_unset"),
        ({"JEV_SHADOW_ENTITY": "Manolii; rm"}, "entity_unset"),
        ({"TYPESAFE_API_KEY": ""}, "credential_unavailable"),
    ],
)
def test_refusals_never_call_provider(tmp_path, over, reason):
    calls: list = []
    got, receipts = shadow.run_shadow(
        **_workspace(tmp_path, {"a": "post"}),
        pr_number=7,
        head_sha=SHA,
        env=_env(**over),
        opener=_opener(ALL_HIGH, sink=calls),
    )
    assert (got, receipts, calls) == (reason, [], [])


def test_cpdcheck_denied_even_with_every_opt_in(tmp_path):
    reason, _ = shadow.run_shadow(
        **_workspace(tmp_path, {"a": "post"}),
        pr_number=7,
        head_sha=SHA,
        env=_env(GITHUB_REPOSITORY_OWNER="CPDcheck", JEV_SHADOW_ENTITY="cpdcheck"),
        opener=_opener(ALL_HIGH),
    )
    assert reason == "owner_denied"


def test_classification_and_payload_free_receipts(tmp_path):
    low = dict(ALL_HIGH, novelty=0.1)
    sent: list = []
    _, receipts = shadow.run_shadow(
        **_workspace(tmp_path, {"kept": "post", "dropped": "drop"}),
        pr_number=7,
        head_sha=SHA,
        env=_env(),
        opener=_opener(low, sink=sent),
    )
    classes = {r["finding_id"]: r["shadow_class"] for r in receipts}
    assert classes == {"kept": "false_drop_candidate", "dropped": "agreement_drop"}
    blob = json.dumps(receipts)
    assert SECRET_MESSAGE not in blob and KEY not in blob and "src.py" not in blob
    assert all(
        r["pinned_model"] == "jev-1.13.0" and r["entity"] == "impaktful"
        for r in receipts
    )
    state = json.loads(sent[0].data)["state"]
    assert "hunter2hunter2" not in state and "[REDACTED]" in state


def test_findings_without_judge_decision_are_not_sent(tmp_path):
    ws = _workspace(tmp_path, {"a": "post"})
    (ws["judge_log_dir"] / f"7-{SHA[:8]}.jsonl").write_text("")
    calls: list = []
    reason, receipts = shadow.run_shadow(
        **ws,
        pr_number=7,
        head_sha=SHA,
        env=_env(),
        opener=_opener(ALL_HIGH, sink=calls),
    )
    assert (reason, receipts, calls) == ("no_judge_decisions", [], [])


@pytest.mark.parametrize(
    "opener",
    [
        _opener(ALL_HIGH, model="jev-latest"),
        _opener({"accuracy": 0.9}),
        _opener(dict(ALL_HIGH, accuracy=1.5)),
    ],
)
def test_invalid_responses_fail_open(tmp_path, opener):
    _, receipts = shadow.run_shadow(
        **_workspace(tmp_path, {"a": "post"}),
        pr_number=7,
        head_sha=SHA,
        env=_env(),
        opener=opener,
    )
    assert receipts[0]["shadow_class"] == "unavailable"
    assert receipts[0]["error_class"] == "ShadowResponseError"


def test_http_error_fails_open_without_body(tmp_path):
    def opener(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 401, "no", {}, io.BytesIO(b"secret body")
        )

    _, receipts = shadow.run_shadow(
        **_workspace(tmp_path, {"a": "post"}),
        pr_number=7,
        head_sha=SHA,
        env=_env(),
        opener=opener,
    )
    assert receipts[0]["error_class"] == "ShadowHTTPError"
    assert "secret body" not in json.dumps(receipts)


def test_non_https_base_url_refused(tmp_path):
    calls: list = []
    _, receipts = shadow.run_shadow(
        **_workspace(tmp_path, {"a": "post"}),
        pr_number=7,
        head_sha=SHA,
        env=_env(TYPESAFE_BASE_URL="http://evil.example"),
        opener=_opener(ALL_HIGH, sink=calls),
    )
    assert calls == [] and receipts[0]["shadow_class"] == "unavailable"


def test_time_budget_stops_calls(tmp_path):
    ticks = iter([0.0] + [shadow.MAX_SHADOW_SECONDS + 1] * 20)
    calls: list = []
    _, receipts = shadow.run_shadow(
        **_workspace(tmp_path, {"a": "post", "b": "drop"}),
        pr_number=7,
        head_sha=SHA,
        env=_env(),
        opener=_opener(ALL_HIGH, sink=calls),
        clock=lambda: next(ticks),
    )
    assert calls == [] and {r["error_class"] for r in receipts} == {"TimeoutError"}


def test_excerpt_cannot_escape_workspace(tmp_path):
    (tmp_path / "ws").mkdir()
    (tmp_path / "outside.txt").write_text("outside")
    assert shadow._excerpt(tmp_path / "ws", "../outside.txt", 1)[0] == "unavailable"
    assert shadow._excerpt(tmp_path / "ws", "/etc/passwd", 1)[0] == "unavailable"


def test_main_always_exits_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_ENABLED_JUDGE_FINDING_SHADOW", "1")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(shadow, "run_shadow", lambda **_: 1 / 0)
    assert (
        shadow.main(["--pr-number", "7", "--sha", SHA, "--output", str(tmp_path / "o")])
        == 0
    )


def _judge_steps() -> dict[str, dict]:
    jobs = yaml.safe_load(REUSABLE.read_text())["jobs"]
    return {s.get("name", ""): s for s in jobs["judge"]["steps"]}


def test_workflow_shadow_is_opt_in_fail_open_and_uses_pack_copy():
    steps = _judge_steps()
    checkout = steps["Check out pack Jev shadow runner"]
    run = steps["Jev judge shadow (observe-only)"]
    upload = steps["Upload Jev judge shadow receipts"]
    assert "inputs.jev_judge_shadow == 'on'" in checkout["if"]
    assert "vars.JEV_ENABLED_JUDGE_FINDING_SHADOW == 'true'" in checkout["if"]
    assert checkout["with"]["repository"] == "Manolii-org/ai-starter-pack"
    assert checkout["with"]["persist-credentials"] is False
    assert all(s.get("continue-on-error") is True for s in (checkout, run, upload))
    assert ".pack-jev/scripts/jev_judge_shadow.py" in run["run"]
    key_expr = run["env"]["TYPESAFE_API_KEY"]
    assert "inputs.jev_judge_shadow == 'on'" in key_expr
    assert "secrets.JEV_TYPESAFE_API_KEY" in key_expr


def test_workflow_shadow_defaults_off():
    inputs = yaml.safe_load(REUSABLE.read_text())[True]["workflow_call"]["inputs"]
    assert inputs["jev_judge_shadow"]["default"] == "off"
    assert inputs["jev_entity"]["default"] == ""
