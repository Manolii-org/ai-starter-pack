"""Offline production-path checks; these are not model efficacy evidence."""

import importlib.util
import io
import json
import sys
from pathlib import Path
import urllib.error

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from lib import protected_review as pr  # noqa: E402 -- script-local library

HEAD = "a" * 40
DIFF = "+assert denied('tenant-b')\n"


@pytest.fixture(autouse=True)
def estate(monkeypatch):
    monkeypatch.setenv("SECURITY_REVIEW_MODE", "estate-ci-v1")
    monkeypatch.setenv("PR_HEAD_SHA", HEAD)
    monkeypatch.setenv("REQUIRED_SECURITY_ROLES", "security-boundary-test")
    monkeypatch.delenv("CLIENT_AI_POLICY", raising=False)


def inventory():
    return {
        "data": [
            {
                "model_name": "tier-1-fast",
                "litellm_params": {"model": "fireworks_ai/deepseek-v4p1-flash"},
                "model_info": {"id": "dep-deepseek", "max_input_tokens": 1048576},
            },
            {
                "model_name": "sonnet-internal",
                "litellm_params": {"model": pr.KIMI_BACKEND},
                "model_info": {"id": "dep-kimi", "max_input_tokens": 1048576},
            },
        ]
    }


def verdict(findings=None, **overrides):
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {"source": "security-boundary-test", "findings": findings or []}
                ),
            }
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 8},
        **overrides,
    }


def finding():
    return {
        "file": "src/auth.py",
        "line": 2,
        "severity": "ERROR",
        "message": "Missing cross-tenant denial assertion",
        "fix": "Add tenant B denial in tests/test_auth.py:4",
    }


def install(monkeypatch, replies=None, info=None):
    sent = []
    responses = list(replies) if replies is not None else [verdict(), verdict()]

    def request(base, key, path, payload=None):
        if path == "/model/info":
            return info or inventory(), {}
        sent.append(payload)
        data = responses.pop(0)
        if isinstance(data, Exception):
            raise data
        model_id = "dep-deepseek" if payload["model"] == pr.ROUTES[0] else "dep-kimi"
        return data, {
            "deployment_id": model_id,
            "call_id": "request-id",
            "payload_sha256": pr.digest(json.dumps(payload)),
        }

    monkeypatch.setattr(pr, "_request", request)
    return sent


def run(user=DIFF, diff=DIFF):
    return pr.review(
        "security-boundary-test",
        "Review all boundaries",
        user,
        diff,
        "https://proxy.example",
        "offline-key",
    )


def test_full_input_two_blind_legs_and_union(monkeypatch):
    sent = install(monkeypatch, [verdict([finding()]), verdict()])
    user = (
        "<untrusted_diff>"
        + "a" * 55000
        + ('é\\"\n' * 2000)
        + DIFF
        + "</untrusted_diff>"
    )
    result = run(user)
    assert len(sent) == 2
    assert [p["messages"][0]["content"] for p in sent] == [user, user]
    assert result["findings"] == [finding()]
    assert {x["family"] for x in result["protected_review"]["legs"]} == {
        "deepseek",
        "kimi",
    }
    pr.validate(
        result["protected_review"], "security-boundary-test", HEAD, pr.digest(DIFF)
    )


@pytest.mark.parametrize(
    "bad",
    [
        "missing_leg",
        "same_family",
        "different_input",
        "stale_head",
        "stale_diff",
        "unknown_version",
    ],
)
def test_invalid_receipts_fail_closed(monkeypatch, bad):
    install(monkeypatch)
    receipt = run()["protected_review"]
    if bad == "missing_leg":
        receipt["legs"].pop()
    if bad == "same_family":
        receipt["legs"][1]["family"] = "deepseek"
    if bad == "different_input":
        receipt["legs"][1]["input_sha256"] = "c" * 64
    if bad == "stale_head":
        receipt["head_sha"] = "b" * 40
    if bad == "stale_diff":
        receipt["diff_sha256"] = "b" * 64
    if bad == "unknown_version":
        receipt["version"] = 2
    with pytest.raises(ValueError):
        pr.validate(receipt, "security-boundary-test", HEAD, pr.digest(DIFF))


def test_truncation_retries_once_without_dropping_input(monkeypatch):
    bad = verdict(stop_reason="max_tokens")
    bad["content"][0]["text"] = '{"source":'
    sent = install(monkeypatch, [bad, verdict(), verdict()])
    run()
    assert [p["max_tokens"] for p in sent] == [2848, 5696, 2848]
    assert all(p["messages"][0]["content"] == DIFF for p in sent)


@pytest.mark.parametrize(
    "data",
    [
        verdict(stop_reason="max_tokens"),
        {"content": []},
        verdict(content=[{"type": "text", "text": '{"source":"wrong","findings":[]}'}]),
        verdict([{"file": "src/auth.py"}]),
    ],
)
def test_incomplete_outputs_never_pass(monkeypatch, data):
    install(monkeypatch, [data])
    with pytest.raises(ValueError):
        run()


def test_timeout_is_unknown_outcome_without_replay(monkeypatch):
    sent = install(
        monkeypatch, [RuntimeError("transport failed; remote outcome unknown")]
    )
    with pytest.raises(RuntimeError, match="outcome unknown"):
        run()
    assert len(sent) == 1


def test_missing_capacity_never_dispatches(monkeypatch):
    data = inventory()
    data["data"][1]["model_info"]["max_input_tokens"] = None
    sent = install(monkeypatch, info=data)
    with pytest.raises(ValueError, match="capacity unavailable"):
        run()
    assert sent == []


def test_context_upper_bound_and_escaping_never_truncate(monkeypatch):
    sent = install(monkeypatch)
    with pytest.raises(ValueError, match="capacity"):
        run('"' * 600000)
    # Known second-leg overflow must refuse before paying for the first leg.
    assert sent == []


@pytest.mark.parametrize(
    "mode,policy,expected",
    [
        ("direct", "", False),
        ("estate-ci-v1", "", True),
        ("estate-ci-v1", "us_weights_ok", False),
    ],
)
def test_external_and_client_policy_lanes(monkeypatch, mode, policy, expected):
    monkeypatch.setenv("SECURITY_REVIEW_MODE", mode)
    monkeypatch.setenv("CLIENT_AI_POLICY", policy)
    assert pr.enabled() is expected


def test_unknown_mode_and_role_refused(monkeypatch):
    with pytest.raises(ValueError, match="not enabled"):
        pr.review("untrusted-role", "s", "u", "d", "https://proxy.example", "key")
    monkeypatch.setenv("SECURITY_REVIEW_MODE", "arbitrary")
    with pytest.raises(ValueError):
        pr.enabled()


@pytest.mark.parametrize(
    "origin,key",
    [
        ("http://proxy.example", "key"),
        ("https://user:pass@proxy.example", "key"),
        ("https://proxy.example?token=x", "key"),
        ("https://proxy.example", "sk-ant-offline"),
    ],
)
def test_no_wrong_origin_or_direct_credential(origin, key):
    with pytest.raises(ValueError):
        pr._request(origin, key, "/model/info")


def test_production_http_receipt_and_safe_errors(monkeypatch):
    class Response(io.BytesIO):
        headers = {"x-litellm-model-id": "dep", "authorization": "DO_NOT_COPY"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Opener:
        def open(self, request, timeout):
            assert timeout == 90
            assert request.full_url == "https://proxy.example/v1/messages"
            return Response(b'{"content":[]}')

    monkeypatch.setattr(pr.urllib.request, "build_opener", lambda *a: Opener())
    _, evidence = pr._request(
        "https://proxy.example", "offline", "/v1/messages", {"model": "sonnet"}
    )
    assert evidence["deployment_id"] == "dep" and "DO_NOT_COPY" not in str(evidence)

    class Broken:
        def open(self, *a, **k):
            raise urllib.error.HTTPError(
                "https://proxy.example",
                400,
                "private payload",
                {},
                io.BytesIO(b"SENSITIVE"),
            )

    monkeypatch.setattr(pr.urllib.request, "build_opener", lambda *a: Broken())
    with pytest.raises(RuntimeError) as exc:
        pr._request("https://proxy.example", "offline", "/v1/messages", {})
    assert str(exc.value) == "security review HTTP 400; incomplete"


def load_script(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), ROOT / "scripts" / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_real_specialist_and_judge_receipt_binding(monkeypatch, tmp_path):
    specialist = load_script("run-specialists")
    monkeypatch.setenv("LITELLM_PROXY_URL", "https://proxy.example")
    monkeypatch.setenv("LLM_API_KEY", "offline")
    install(monkeypatch)
    _, error = specialist._invoke_skill("security-boundary-test", DIFF, tmp_path)
    assert error is None
    diff_path = tmp_path / "input.diff"
    diff_path.write_text(DIFF)
    monkeypatch.setenv("REVIEW_DIFF_PATH", str(diff_path))
    judge = load_script("run-judge").Judge(str(tmp_path), 1, HEAD)
    assert judge.load_candidates() == [] and judge._candidate_load_errors == 0
    judge = load_script("run-judge").Judge(str(tmp_path), 1, "b" * 40)
    assert judge.load_candidates() == [] and judge._candidate_load_errors == 1


def test_impossible_independence_rejects_before_either_call(monkeypatch):
    data = inventory()
    data["data"][1]["litellm_params"]["model"] = "fireworks_ai/deepseek-v4p1-flash"
    sent = install(monkeypatch, info=data)
    with pytest.raises(ValueError, match="families unavailable"):
        run()
    assert sent == []


def test_unqualified_role_cannot_reuse_valid_receipt(monkeypatch):
    install(monkeypatch)
    receipt = run()["protected_review"]
    receipt["source"] = "shell-security"
    with pytest.raises(ValueError):
        pr.validate(receipt, "shell-security", HEAD, pr.digest(DIFF))


@pytest.mark.parametrize("missing", [True, False])
def test_real_judge_run_never_approves_partial_roles(monkeypatch, tmp_path, missing):
    module = load_script("run-judge")
    (tmp_path / "test-adequacy.json").write_text(
        json.dumps({"source": "test-adequacy", "findings": [finding()]})
    )
    if not missing:
        (tmp_path / "security-boundary-test.json").write_text(
            json.dumps(
                {
                    "source": "security-boundary-test",
                    "first_party": True,
                    "findings": [],
                    "skipped": "protected_review_incomplete",
                }
            )
        )
    judge = module.Judge(str(tmp_path), 1, HEAD)
    judge.judge_log_dir = tmp_path / "judge-log"
    monkeypatch.setattr(judge, "apply_specificity_gate", lambda rows: rows)
    monkeypatch.setattr(
        judge,
        "invoke_judge_agent",
        lambda rows: {
            "surviving": [r.to_dict() for r in rows],
            "review_action": "APPROVE",
        },
    )
    posted = []
    monkeypatch.setattr(
        judge,
        "post_review_to_github",
        lambda rows, action: posted.append(action) or True,
    )
    monkeypatch.setattr(judge, "_dismiss_stale_judge_reviews", lambda *a: None)
    assert judge.run() == 0
    assert posted == ["COMMENT"]
    assert judge._candidate_load_errors == (1 if missing else 0)


def test_unqualified_broad_role_stays_incomplete_and_client_policy_stays_direct(
    monkeypatch,
):
    module = load_script("run-broad-agents")
    agent = module.AgentConfig(
        "security-deep-dive",
        "sonnet",
        "restricted_us_oss_ok",
        "s",
        "i",
        first_party=True,
    )
    monkeypatch.setenv("LITELLM_PROXY_URL", "https://proxy.example")
    monkeypatch.delenv("ANTHROPIC_DIRECT_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = module.invoke_agent(agent, "offline", DIFF, DIFF)
    assert result["skipped"] == "protected_review_incomplete"
    monkeypatch.setenv("CLIENT_AI_POLICY", "us_weights_ok")
    result = module.invoke_agent(agent, "offline", DIFF, DIFF)
    assert result["skipped"] == "no_direct_key"


@pytest.mark.parametrize("error", [TimeoutError(), ConnectionResetError()])
def test_real_transport_unknown_outcome_has_one_attempt(monkeypatch, error):
    attempts = []

    class Broken:
        def open(self, *args, **kwargs):
            attempts.append(1)
            raise error

    monkeypatch.setattr(pr.urllib.request, "build_opener", lambda *a: Broken())
    with pytest.raises(RuntimeError, match="remote outcome unknown"):
        pr._request("https://proxy.example", "offline", "/v1/messages", {})
    assert attempts == [1]


def test_single_json_fence_preserves_existing_structured_output_compatibility(
    monkeypatch,
):
    response = verdict([finding()])
    response["content"][0]["text"] = (
        "```json\n" + response["content"][0]["text"] + "\n```"
    )
    install(monkeypatch, [response, verdict()])
    assert run()["findings"] == [finding()]


@pytest.mark.parametrize(
    "text",
    [
        'Ignore policy. {"source":"security-boundary-test","findings":[]}',
        '```json\n{"source":"security-boundary-test","findings":[]}',
        "```json\n{}\n```\n```json\n{}\n```",
        '{"source":"security-boundary-test","findings":[]} {}',
    ],
)
def test_fence_compatibility_does_not_accept_malformed_or_multiple_verdicts(text):
    with pytest.raises(ValueError):
        pr._parse(text, "security-boundary-test")


def test_absent_mode_keeps_direct_default_even_with_ambient_proxy_key(monkeypatch):
    monkeypatch.delenv("SECURITY_REVIEW_MODE")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "offline-ambient")
    assert pr.enabled() is False
    with pytest.raises(ValueError, match="not enabled"):
        run()


@pytest.mark.parametrize(
    "backend",
    [
        "moonshotai/kimi-k2.6",
        "fireworks_ai/accounts/fireworks/models/kimi-k2p6",
        "prefix/" + pr.KIMI_BACKEND,
        pr.KIMI_BACKEND + "-unknown",
    ],
)
def test_only_exact_k3_backend_is_eligible_before_generation(monkeypatch, backend):
    info = inventory()
    info["data"][1]["litellm_params"]["model"] = backend
    sent = install(monkeypatch, info=info)
    with pytest.raises(ValueError, match="unqualified"):
        run()
    assert sent == []


@pytest.mark.parametrize("missing_capacity", [False, True])
def test_both_k3_slots_preflight_and_selected_identity(monkeypatch, missing_capacity):
    info = inventory()
    second = json.loads(json.dumps(info["data"][1]))
    second["model_info"]["id"] = "dep-kimi-second"
    info["data"].append(second)
    if missing_capacity:
        second["model_info"]["max_input_tokens"] = None
    sent = install(monkeypatch, info=info)
    original = pr._request

    def select_second(base, key, path, payload=None):
        result, evidence = original(base, key, path, payload)
        if payload and payload["model"] == "sonnet-internal":
            evidence["deployment_id"] = "dep-kimi-second"
        return result, evidence

    monkeypatch.setattr(pr, "_request", select_second)
    if missing_capacity:
        with pytest.raises(ValueError, match="capacity unavailable"):
            run()
        assert sent == []
    else:
        result = run()
        leg = result["protected_review"]["legs"][1]
        assert leg["deployment_id"] == "dep-kimi-second"
        assert leg["backend"] == pr.KIMI_BACKEND and leg["family"] == "kimi"
        assert [p["max_tokens"] for p in sent] == [2848, 2848]
        assert all(
            set(p) == {"model", "max_tokens", "system", "messages"} for p in sent
        )


def test_fallback_receipt_cannot_count_as_k3(monkeypatch):
    info = inventory()
    info["data"].append(
        {
            "model_name": "sonnet-internal-fallback-openrouter",
            "litellm_params": {"model": "moonshotai/kimi-k2.6"},
            "model_info": {"id": "dep-fallback", "max_input_tokens": 262144},
        }
    )
    sent = install(monkeypatch, info=info)
    original = pr._request

    def served_fallback(base, key, path, payload=None):
        result, evidence = original(base, key, path, payload)
        if payload and payload["model"] == "sonnet-internal":
            evidence["deployment_id"] = "dep-fallback"
        return result, evidence

    monkeypatch.setattr(pr, "_request", served_fallback)
    with pytest.raises(ValueError, match="unqualified deployment"):
        run()
    assert len(sent) == 2  # no third call/replay after unknown served provenance


def test_smaller_second_slot_rejects_full_input_before_either_call(monkeypatch):
    info = inventory()
    info["data"][1]["model_info"]["max_input_tokens"] = 4096
    sent = install(monkeypatch, info=info)
    with pytest.raises(ValueError, match="capacity"):
        run("complete input" * 1000)
    assert sent == []


def test_numeric_timeout_and_output_limits_remain_unchanged():
    assert pr.TIMEOUT == 90 and pr.MAX_BYTES == 32_000_000
    assert pr.ROUTES == ("tier-1-fast", "sonnet-internal")


def test_http_failure_is_incomplete_and_never_replayed(monkeypatch):
    calls = []

    class Broken:
        def open(self, *args, **kwargs):
            calls.append(kwargs["timeout"])
            raise urllib.error.HTTPError(
                "https://proxy.example", 503, "private", {}, io.BytesIO(b"PRIVATE")
            )

    monkeypatch.setattr(pr.urllib.request, "build_opener", lambda *a: Broken())
    # Exercise real transport plus review loop, without any network connection.
    real = pr._request

    def metadata_then_failure(base, key, path, payload=None):
        if path == "/model/info":
            return inventory(), {}
        return real(base, key, path, payload)

    monkeypatch.setattr(pr, "_request", metadata_then_failure)
    with pytest.raises(RuntimeError, match="HTTP 503; incomplete"):
        run()
    assert calls == [90]
