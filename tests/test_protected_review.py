"""Offline production-path checks; these are not model efficacy evidence."""

import importlib.util
import hashlib
import io
import json
import sys
import subprocess
from pathlib import Path
import urllib.error
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from lib import protected_review as pr  # noqa: E402 -- script-local library

HEAD = "a" * 40
DIFF = "+assert denied('tenant-b')\n"


@pytest.fixture(autouse=True)
def deny_unmocked_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("unmocked live HTTP forbidden in offline tests")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", denied)


@pytest.fixture(autouse=True)
def estate(monkeypatch):
    # Unit fixtures must not inherit the CI launcher; hosted cases set explicit context.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
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


def install(monkeypatch, replies=None, info=None, helper=pr):
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

    monkeypatch.setattr(helper, "_request", request)
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
    install(monkeypatch, helper=specialist.protected_review)
    _, error = specialist._invoke_skill("security-boundary-test", DIFF, tmp_path)
    assert error is None
    diff_path = tmp_path / "input.diff"
    diff_path.write_text(DIFF)
    monkeypatch.setenv("REVIEW_DIFF_PATH", str(diff_path))
    judge = load_script("run-judge").Judge(str(tmp_path), 1, HEAD)
    assert judge.load_candidates() == [] and judge._candidate_load_errors == 0
    judge = load_script("run-judge").Judge(str(tmp_path), 1, "b" * 40)
    assert judge.load_candidates() == [] and judge._candidate_load_errors == 1


def load_identity_receipt(monkeypatch, tmp_path, first, second, *, aggregate_duplicates=False, module=None):
    """Actual v1 producer receipt and Judge loader; never a model/network call."""
    install(monkeypatch, [verdict(first), verdict(second)])
    result = run()
    if aggregate_duplicates:
        result["findings"] *= 2
    diff_path = tmp_path / "identity-input.diff"
    diff_path.write_text(DIFF)
    monkeypatch.setenv("REVIEW_DIFF_PATH", str(diff_path))
    candidates = tmp_path / "identity-candidates"
    candidates.mkdir()
    path = candidates / "security-boundary-test.json"
    original = json.dumps(result).encode()
    path.write_bytes(original)
    module = module or load_script("run-judge")
    judge = module.Judge(str(candidates), 1, HEAD)
    judge.judge_log_dir = tmp_path / "identity-logs"
    loaded = judge.load_candidates()
    assert path.read_bytes() == original  # Historical/original v1 receipt is immutable.
    assert result["protected_review"]["version"] == 1
    return module, judge, loaded, result


@pytest.mark.parametrize("left_id,right_id,second_present,expected_count,expected_investigations", [
    ("original-id", "original-id", False, 1, 1),
    ("original-id", "original-id", True, 1, 0),
    ("original-id", "other-original-id", True, 2, 0),
    ("original-id", None, True, 2, 0),
    (None, None, True, 1, 0),
    (None, None, False, 1, 1),
])
def test_protected_identity_agreement_uses_content_not_producer_ids(
    monkeypatch, tmp_path, left_id, right_id, second_present, expected_count, expected_investigations
):
    left, right = finding(), finding()
    if left_id is not None:
        left["finding_id"] = left_id
    if right_id is not None:
        right["finding_id"] = right_id
    module, judge, loaded, _ = load_identity_receipt(
        monkeypatch, tmp_path, [left], [right] if second_present else [])
    assert judge._candidate_load_errors == 0 and len(loaded) == expected_count
    assert left_id is None or loaded[0].finding_id == left_id
    investigations = module._investigations(loaded, judge._review_observations)
    assert len(investigations) == expected_investigations
    if investigations:
        assert investigations[0]["status"] == "MUST_INVESTIGATE"
        assert set(investigations[0]["finding_ids"]) == {f.finding_id for f in loaded}


@pytest.mark.parametrize("bad", [False, 0, 7, [], {}, "", "   "])
def test_malformed_protected_id_is_explicit_incomplete_without_partial_observations(monkeypatch, tmp_path, bad):
    raw = dict(finding(), finding_id=bad)
    module, judge, loaded, _ = load_identity_receipt(monkeypatch, tmp_path, [finding(), raw], [])
    assert loaded == [] and judge._candidate_load_errors == 1
    assert judge._review_observations == []
    monkeypatch.setattr(judge, "load_candidates", lambda: loaded)
    monkeypatch.setattr(judge, "invoke_judge_agent", lambda *_: pytest.fail("invalid artifact dispatch"))
    monkeypatch.setattr(judge, "_post_no_findings_comment", lambda: pytest.fail("false clean"))
    posted = []
    monkeypatch.setattr(judge, "_post_advisory_warning", lambda detail: posted.append(detail) or False)
    assert judge.run() == 0 and posted


def test_exact_duplicate_protected_candidates_keep_one_id_and_original_provenance(monkeypatch, tmp_path):
    raw = dict(finding(), finding_id="historical-id")
    _, judge, loaded, original = load_identity_receipt(
        monkeypatch, tmp_path, [raw, raw], [raw], aggregate_duplicates=True)
    assert len(loaded) == 1 and loaded[0].finding_id == "historical-id"
    assert judge._candidate_load_errors == 0
    assert len(original["protected_review"]["legs"][0]["findings"]) == 2


@pytest.mark.parametrize("field,value", [("message", "Different concrete defect"), ("fix", "Different proposed repair"), ("severity", "WARNING")])
def test_distinct_findings_with_colliding_ids_are_incomplete_not_merged(monkeypatch, tmp_path, field, value):
    first = dict(finding(), finding_id="collision")
    second = dict(first, **{field: value})
    _, judge, loaded, original = load_identity_receipt(monkeypatch, tmp_path, [first], [second])
    assert loaded == [] and judge._candidate_load_errors == 1
    assert judge._review_observations == []
    assert len(original["findings"]) == 2


@pytest.mark.parametrize("field,value", [("message", "Different concrete defect"), ("fix", "Different proposed repair"), ("severity", "WARNING")])
def test_distinct_findings_with_distinct_ids_require_disagreement_investigation(monkeypatch, tmp_path, field, value):
    first = dict(finding(), finding_id="first-original")
    second = dict(first, finding_id="second-original", **{field: value})
    module, judge, loaded, _ = load_identity_receipt(monkeypatch, tmp_path, [first], [second])
    assert len(loaded) == 2 and judge._candidate_load_errors == 0
    investigations = module._investigations(loaded, judge._review_observations)
    assert len(investigations) == 1
    assert set(investigations[0]["finding_ids"]) == {"first-original", "second-original"}


@pytest.mark.parametrize("identity", ["missing", None, "  original-id  "])
def test_historical_v1_ids_keep_legacy_synthesis_or_exact_original(monkeypatch, tmp_path, identity):
    raw = finding()
    if identity != "missing":
        raw["finding_id"] = identity
    module, judge, loaded, _ = load_identity_receipt(monkeypatch, tmp_path, [raw], [])
    expected = identity if isinstance(identity, str) and identity != "missing" else hashlib.sha256(
        f"security-boundary-test|{raw['file']}|{raw['line']}|{raw['message']}".encode()).hexdigest()[:12]
    assert loaded[0].finding_id == expected
    assert judge._review_observations[0]["finding_ids"] == [expected]
    assert module._investigations(loaded, judge._review_observations)[0]["finding_ids"] == [expected]
    assert set(loaded[0].to_dict()) == {"finding_id", "source", "file", "line", "severity", "message", "fix", "confidence"}


@pytest.mark.parametrize("field,value", [("fix", "Different repair"), ("severity", "WARNING")])
def test_legacy_id_collision_cannot_merge_distinct_content(monkeypatch, tmp_path, field, value):
    first, second = finding(), dict(finding(), **{field: value})
    _, judge, loaded, original = load_identity_receipt(monkeypatch, tmp_path, [first], [second])
    assert not loaded and judge._candidate_load_errors == 1
    assert len(original["findings"]) == 2 and not judge._review_observations


def test_equal_content_different_ids_and_confidence_retains_both_provenances(monkeypatch, tmp_path):
    first = dict(finding(), finding_id="first-id", confidence=0.9)
    second = dict(finding(), finding_id="second-id", confidence=0.6)
    module, judge, loaded, original = load_identity_receipt(monkeypatch, tmp_path, [first], [second])
    assert [f.finding_id for f in loaded] == ["first-id", "second-id"]
    assert [f.confidence for f in loaded] == [0.9, 0.6]
    assert not module._investigations(loaded, judge._review_observations)
    assert original["protected_review"]["legs"][0]["findings"] == [first]


@pytest.mark.parametrize("identity", [None, "original-id"])
@pytest.mark.parametrize("scores,expected_confidence", [
    ((0.9, 0.6), 0.9), ((0.6, 0.9), 0.6), (("high", "medium"), 0.9), (("medium", "high"), 0.5),
])
def test_equal_identity_content_different_confidence_keeps_first_original_and_both_legs(
    monkeypatch, tmp_path, identity, scores, expected_confidence
):
    first, second = dict(finding(), confidence=scores[0]), dict(finding(), confidence=scores[1])
    if identity is not None:
        first["finding_id"] = second["finding_id"] = identity
    module, judge, loaded, original = load_identity_receipt(monkeypatch, tmp_path, [first], [second])
    assert judge._candidate_load_errors == 0 and len(loaded) == 1
    assert loaded[0].to_dict() == dict(finding(), source="security-boundary-test",
                                     finding_id=identity or "bd16f754c699", confidence=expected_confidence)
    assert not module._investigations(loaded, judge._review_observations)
    assert original["findings"] == [first, second]
    assert [leg["findings"] for leg in original["protected_review"]["legs"]] == [[first], [second]]


@pytest.mark.parametrize("layout", ["", "plugin/manolii-framework/", "registry/platform/framework/"])
@pytest.mark.parametrize("identity", [None, "original-id"])
def test_protected_identity_is_stable_in_all_shipped_judges(monkeypatch, tmp_path, layout, identity):
    path = ROOT / layout / "scripts/run-judge.py"
    spec = importlib.util.spec_from_file_location("identity_judge_" + layout.replace("/", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    raw = finding() if identity is None else dict(finding(), finding_id=identity)
    _, judge, loaded, _ = load_identity_receipt(monkeypatch, tmp_path, [raw], [], module=module)
    assert loaded[0].to_dict() == dict(finding(), source="security-boundary-test",
                                     finding_id=identity or "bd16f754c699", confidence=0.5)
    assert len(module._investigations(loaded, judge._review_observations)) == 1


def test_colliding_later_artifact_keeps_prior_valid_candidate_and_incomplete_state(monkeypatch, tmp_path):
    monkeypatch.setenv("SECURITY_REVIEW_MODE", "direct")
    module = load_script("run-judge")
    for name, source in [("a.json", "first-source"), ("z.json", "second-source")]:
        (tmp_path / name).write_text(json.dumps({"source": source, "findings": [dict(finding(), finding_id="collision")]}))
    judge = module.Judge(str(tmp_path), 1, HEAD)
    loaded = judge.load_candidates()
    assert len(loaded) == 1 and loaded[0].source == "first-source"
    assert judge._candidate_load_errors == 1


def test_direct_ambiguous_identity_refuses_before_endpoint_or_model_dispatch(monkeypatch):
    module = load_script("run-judge")
    monkeypatch.setattr(module, "_endpoint", lambda **kw: pytest.fail("identity ambiguity reached route selection"))
    first = module.Finding.from_record("review", dict(finding(), finding_id="collision"))
    second = module.Finding.from_record("review", dict(finding(), finding_id="collision", message="Distinct defect"))
    judge = module.Judge("unused", 1, HEAD)
    assert judge.invoke_judge_agent([first, second]) is None and judge._adjudication_incomplete


def test_historical_observation_alias_cannot_hide_one_leg_disagreement():
    module = load_script("run-judge")
    item = module.Finding.from_record("security-boundary-test", dict(finding(), finding_id="original-id"))
    observations = [{"finding_ids": [item.legacy_id()]}, {"finding_ids": []}]
    result = module._investigations([item], observations)
    assert len(result) == 1 and result[0]["finding_ids"] == ["original-id"]


@pytest.mark.parametrize("field,value", [("fix", "Different repair"), ("severity", "WARNING")])
def test_ambiguous_historical_alias_is_not_evidence_of_agreement(field, value):
    module = load_script("run-judge")
    first = module.Finding.from_record("review", dict(finding(), finding_id="first"))
    second = module.Finding.from_record("review", dict(finding(), finding_id="second", **{field: value}))
    assert first.legacy_id() == second.legacy_id()
    observations = [{"finding_ids": [first.legacy_id()]}, {"finding_ids": [second.legacy_id()]}]
    assert len(module._investigations([first, second], observations)) == 1


def test_malformed_historical_observation_cannot_count_as_agreement():
    module = load_script("run-judge")
    item = module.Finding.from_record("review", finding())
    assert len(module._investigations([item], [{"finding_ids": [False]}, {"finding_ids": []}])) == 1


def test_cross_location_public_id_cannot_impersonate_legacy_alias():
    module = load_script("run-judge")
    first = module.Finding.from_record("review", dict(finding(), finding_id="first"))
    second = module.Finding.from_record("review", dict(finding(), file="src/other.py", finding_id=first.legacy_id()))
    observations = [{"finding_ids": [first.finding_id]}, {"finding_ids": [second.finding_id]}]
    investigations = module._investigations([first, second], observations)
    assert len(investigations) == 2
    assert {tuple(row["finding_ids"]) for row in investigations} == {(first.finding_id,), (second.finding_id,)}


@pytest.mark.parametrize("first_party", [False, True])
def test_skipped_protected_artifact_cannot_admit_unverified_findings(monkeypatch, tmp_path, first_party):
    module = load_script("run-judge")
    path = tmp_path / "security-boundary-test.json"
    original = json.dumps({"source": "security-boundary-test", "skipped": True,
                           "first_party": first_party, "findings": [finding()]}).encode()
    path.write_bytes(original)
    judge = module.Judge(str(tmp_path), 1, HEAD)
    loaded = judge.load_candidates()
    assert loaded == [] and judge._candidate_load_errors == 1
    assert not judge._review_observations and not judge._has_first_party_candidates
    assert path.read_bytes() == original
    monkeypatch.setattr(judge, "load_candidates", lambda: loaded)
    monkeypatch.setattr(module, "_endpoint", lambda **kw: pytest.fail("unverified findings reached route selection"))
    monkeypatch.setattr(judge, "invoke_judge_agent", lambda *_: pytest.fail("unverified findings dispatched"))
    monkeypatch.setattr(judge, "_post_no_findings_comment", lambda: pytest.fail("false clean"))
    posted = []
    monkeypatch.setattr(judge, "_post_advisory_warning", lambda detail: posted.append(detail) or False)
    assert judge.run() == 0 and posted


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
    monkeypatch, tmp_path,
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
    result = module.invoke_agent(agent, "offline", DIFF)
    assert result["skipped"] == "protected_review_incomplete"
    # The real direct-admission policy still runs before credential lookup.
    # Supply an actually committed eligible external-consumer policy, rather
    # than bypassing it to reach the no-direct-key branch under test.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", "ExternalExample/project")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    policy = tmp_path / ".ai/capabilities.json"
    policy.parent.mkdir()
    policy.write_text(json.dumps({"version": 1, "mode": "capabilities",
        "capabilities": ["llm-anthropic"], "consumer_scope": "external",
        "consumer_repository": "ExternalExample/project"}))
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "add", ".ai/capabilities.json"], check=True)
    subprocess.run(["git", "-c", "user.email=offline@example.invalid", "-c", "user.name=Offline",
                    "commit", "-qm", "External policy fixture"], check=True)
    monkeypatch.setenv("CLIENT_AI_POLICY", "us_weights_ok")
    result = module.invoke_agent(agent, "offline", DIFF)
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


@pytest.mark.parametrize("failure", ["missing_helper", "unknown_mode", "unqualified_role", "no_proxy", "no_key", "timeout"])
def test_canonical_estate_specialist_fails_closed_without_direct_fallback(monkeypatch, tmp_path, failure):
    specialist = load_script("run-specialists")
    monkeypatch.setenv("LITELLM_PROXY_URL", "https://proxy.example")
    monkeypatch.setenv("LLM_API_KEY", "offline-proxy-key")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(specialist, "_call_api", lambda *a, **k: pytest.fail("direct fallback"))
    calls = []
    helper = specialist.protected_review
    if failure == "missing_helper":
        monkeypatch.setattr(specialist, "protected_review", None)
    elif failure == "unknown_mode":
        monkeypatch.setenv("SECURITY_REVIEW_MODE", "unsupported")
    elif failure == "no_proxy":
        monkeypatch.delenv("LITELLM_PROXY_URL")
    elif failure == "no_key":
        monkeypatch.delenv("LLM_API_KEY")
    elif failure == "timeout":
        def broken(base, key, path, payload=None):
            if payload is None:
                return inventory(), {}
            calls.append(payload)
            raise TimeoutError("PRIVATE_PROVIDER_PAYLOAD")
        monkeypatch.setattr(helper, "_request", broken)
    role = "shell-security" if failure == "unqualified_role" else "security-boundary-test"
    _, error = specialist._invoke_skill(role, DIFF, tmp_path)
    result = json.loads((tmp_path / (role + ".json")).read_text())
    assert result == {"source": role, "findings": [], "first_party": True,
                      "skipped": "protected_review_incomplete"}
    assert "PRIVATE_PROVIDER_PAYLOAD" not in error
    assert len(calls) == (1 if failure == "timeout" else 0)


@pytest.mark.parametrize("truncated_first", [False, True])
@pytest.mark.parametrize("producer_id", [None, "original-producer-id"])
@pytest.mark.parametrize("disagree", [False, True])
def test_real_canonical_specialist_to_source_grounded_judge(monkeypatch, tmp_path, truncated_first, producer_id, disagree):
    root = tmp_path / "subject"
    root.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args]).decode().strip()
    git("init", "-q")
    git("-c", "user.email=offline@example.invalid", "-c", "user.name=Offline",
        "commit", "--allow-empty", "-qm", "Empty source baseline")
    base = git("rev-parse", "HEAD")
    (root / "src").mkdir()
    (root / "src/auth.py").write_text("def permitted(tenant):\n    return tenant == 'owner'\n# FINAL_SOURCE_MARKER\n")
    git("add", "src/auth.py")
    git("-c", "user.email=offline@example.invalid", "-c", "user.name=Offline",
        "commit", "-qm", "Synthetic policy implementation")
    head = git("rev-parse", "HEAD")
    diff = subprocess.check_output(["git", "-C", str(root), "diff", base, head]).decode()
    diff_path = tmp_path / "complete.diff"
    diff_path.write_text(diff)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(root))
    monkeypatch.setenv("PR_HEAD_SHA", head)
    monkeypatch.setenv("PR_BASE_SHA", base)
    monkeypatch.setenv("REVIEW_DIFF_PATH", str(diff_path))
    monkeypatch.setenv("LITELLM_PROXY_URL", "https://proxy.example")
    monkeypatch.setenv("LLM_API_KEY", "offline-proxy-key")
    specialist = load_script("run-specialists")
    raw = finding() if producer_id is None else dict(finding(), finding_id=producer_id)
    sent = install(monkeypatch, [verdict([raw]), verdict([] if disagree else [raw])],
                   helper=specialist.protected_review)
    candidates = tmp_path / "candidates"
    candidates.mkdir()
    assert specialist._invoke_skill("security-boundary-test", diff, candidates) == ("security-boundary-test", None)
    assert [p["model"] for p in sent] == list(pr.ROUTES)
    assert sent[0]["messages"] == sent[1]["messages"]
    assert "FINAL_SOURCE_MARKER" in sent[0]["messages"][0]["content"]
    module = load_script("run-judge")
    judge = module.Judge(str(candidates), 1, head)
    findings = judge.load_candidates()
    assert len(findings) == 1 and judge._candidate_load_errors == 0
    assert producer_id is None or findings[0].finding_id == producer_id
    assert not judge._has_first_party_candidates
    requests = []
    class Reply(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): return False
    def judge_reply(request, *, timeout, host):
        assert host == "proxy.example" and timeout == 120
        payload = json.loads(request.data)
        requests.append(payload)
        packet = json.loads(payload["messages"][0]["content"].split("<untrusted_review_data>\n", 1)[1].split("\n</untrusted_review_data>", 1)[0])
        assert "_comparison_keys" not in json.dumps(packet)
        assert packet["candidates"] == [f.to_dict() for f in findings]
        locations = list(packet["source_evidence"]["locations"])
        answer = {"version": 2, "decisions": [
            {"finding_id": f["finding_id"], "decision": "keep", "gate": "passed",
             "reason": "The complete tracked source has no tenant denial test assertion.", "evidence_ids": locations}
            for f in packet["candidates"]], "investigations": [
                {"id": investigation["id"], "status": "resolved", "reason": "Both retained observations were checked against the exact source.", "evidence_ids": locations}
                for investigation in packet["investigations"]]}
        assert len(packet["investigations"]) == int(disagree)
        return Reply(json.dumps({
            "stop_reason": "max_tokens" if truncated_first and len(requests) == 1 else "end_turn",
            "content": [{"type": "text", "text": json.dumps(answer)}]}).encode())
    monkeypatch.setattr(module, "_urlopen_https", judge_reply)
    result = judge.invoke_judge_agent(findings)
    assert result["complete"] is True and result["review_action"] == "REQUEST_CHANGES"
    assert len(result["surviving"]) == 1 and result["dropped_count"] == 0
    assert [p["max_tokens"] for p in requests] == ([4000, 6048] if truncated_first else [4000])
    assert all(p["messages"] == requests[0]["messages"] for p in requests)


def test_copied_specialist_companion_uses_source_not_initializer_or_cached_bytecode(monkeypatch, tmp_path):
    import py_compile
    import shutil
    consumer = tmp_path / "consumer"
    for relative in ["scripts/run-specialists.py", "scripts/lib/agent_capabilities.py",
                     ".claude/skills/security-boundary-test/SKILL.md"]:
        dest = consumer / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, dest)
    helper = consumer / "scripts/lib/protected_review.py"
    helper.write_text("raise AssertionError('unchecked cache executed')\n")
    py_compile.compile(str(helper), invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    helper.write_bytes((ROOT / "scripts/lib/protected_review.py").read_bytes())
    (helper.parent / "__init__.py").write_text("raise AssertionError('package initializer executed')\n")
    path = consumer / "scripts/run-specialists.py"
    spec = importlib.util.spec_from_file_location("copied_canonical_specialist", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.protected_review.__file__ == str(helper)
    monkeypatch.setenv("LITELLM_PROXY_URL", "https://proxy.example")
    monkeypatch.setenv("LLM_API_KEY", "offline-proxy-key")
    sent = install(monkeypatch, helper=module.protected_review)
    candidates = tmp_path / "candidates"
    candidates.mkdir()
    assert module._invoke_skill("security-boundary-test", DIFF, candidates) == ("security-boundary-test", None)
    assert len(sent) == 2


def test_older_consumer_missing_companion_keeps_ordinary_proxy_and_marks_estate_gap(monkeypatch, tmp_path):
    import shutil
    consumer = tmp_path / "consumer"
    for relative in ["scripts/run-specialists.py", "scripts/lib/agent_capabilities.py",
                     ".claude/skills/security-boundary-test/SKILL.md",
                     ".claude/skills/test-adequacy/SKILL.md"]:
        dest = consumer / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, dest)
    path = consumer / "scripts/run-specialists.py"
    spec = importlib.util.spec_from_file_location("old_consumer_specialist", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.protected_review is None
    monkeypatch.setenv("LITELLM_PROXY_URL", "https://proxy.example")
    monkeypatch.setenv("LLM_API_KEY", "offline-proxy-key")
    calls = []
    def ordinary(*args, **kwargs):
        calls.append(kwargs)
        return '{"source":"test-adequacy","findings":[]}'
    monkeypatch.setattr(module, "_call_api", ordinary)
    assert module._invoke_skill("test-adequacy", DIFF, tmp_path) == ("test-adequacy", None)
    assert len(calls) == 1 and calls[0]["first_party"] is False
    _, error = module._invoke_skill("security-boundary-test", DIFF, tmp_path)
    assert error and len(calls) == 1
    assert json.loads((tmp_path / "security-boundary-test.json").read_text())["skipped"] == "protected_review_incomplete"
