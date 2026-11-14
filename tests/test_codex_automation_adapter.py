"""Guest adapter tests: synthetic contexts/Unix responses, never a real login."""

from http.server import BaseHTTPRequestHandler
import json
import importlib.util
import os
from pathlib import Path
import socketserver
import sys
import threading
import py_compile
import shutil
import hashlib
import subprocess

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from ci import codex_automation_adapter as adapter


@pytest.fixture
def mounted(monkeypatch, tmp_path):
    value = {"schema": adapter.SCHEMA, "repository": "example/automation", "pull_request": 7,
             "base_sha": "b" * 40, "head_sha": "a" * 40, "role": "assessment",
             "data_sensitivity": "internal", "reasoning_effort": "medium",
             "controller_run_id": 31, "controller_run_attempt": 1,
             "selected_roles": ["scope-adherence", "systems-consistency"]}
    path = tmp_path / "context.json"
    def write(data=None, raw=None):
        if path.exists():
            path.chmod(0o644)
        path.write_text(raw if raw is not None else json.dumps(data if data is not None else value))
        path.chmod(0o444)
    write()
    monkeypatch.setattr(adapter, "CONTEXT", path)
    for key, field in (("GITHUB_REPOSITORY", "repository"), ("PR_NUMBER", "pull_request"),
                       ("PR_BASE_SHA", "base_sha"), ("PR_HEAD_SHA", "head_sha"),
                       ("GITHUB_RUN_ID", "controller_run_id"), ("GITHUB_RUN_ATTEMPT", "controller_run_attempt")):
        monkeypatch.setenv(key, str(value[field]))
    monkeypatch.setenv("CODEX_AUTOMATION_MODE", "required")
    monkeypatch.delenv("CLIENT_AI_POLICY", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    # Simulate the actual credential-free guest, never forward session secrets.
    for key in tuple(os.environ):
        if "DOPPLER" in key or key == "GH_PAT" or key.endswith(
            ("_TOKEN", "_API_KEY", "_MASTER_KEY", "_PRIVATE_KEY", "_SECRET_KEY", "_PASSWORD")):
            monkeypatch.delenv(key, raising=False)
    return value, path, write


def invoke(**kwargs):
    return adapter.complete("trusted system", "complete untrusted input", role="assessment",
                            data_sensitivity="internal", max_tokens=2000, **kwargs)


def test_disabled_mode_never_enables_subscription(monkeypatch):
    monkeypatch.delenv("CODEX_AUTOMATION_MODE", raising=False)
    assert adapter.required() is False
    monkeypatch.setattr(adapter, "context", lambda: pytest.fail("no context or model read"))
    with pytest.raises(adapter.AutomationHold, match="mode_required"):
        invoke()


@pytest.mark.parametrize("value", ["first", "proxy", "false", "REQUIRED", " required"])
def test_unknown_mode_is_a_hold_never_a_provider_fallback(value, monkeypatch):
    monkeypatch.setenv("CODEX_AUTOMATION_MODE", value)
    with pytest.raises(adapter.AutomationHold, match="mode_invalid"):
        adapter.required()


@pytest.mark.parametrize("field,bad", [
    ("schema", "other"), ("repository", "../../other"), ("repository", []),
    ("pull_request", True), ("pull_request", 0), ("base_sha", "branch"),
    ("head_sha", "A" * 40), ("role", "implementation"), ("role", []),
    ("data_sensitivity", "restricted"), ("data_sensitivity", []),
    ("reasoning_effort", "unbounded"), ("reasoning_effort", []),
])
def test_unadmitted_context_does_not_invoke(field, bad, mounted, monkeypatch):
    value, _, write = mounted
    value[field] = bad
    write(value)
    monkeypatch.setattr(adapter.transport, "invoke", lambda *a, **k: pytest.fail("no model"))
    with pytest.raises(adapter.AutomationHold, match="context_invalid"):
        invoke()


@pytest.mark.parametrize("key", ["GITHUB_REPOSITORY", "PR_NUMBER", "PR_BASE_SHA", "PR_HEAD_SHA"])
def test_cross_revision_context_is_held(key, mounted, monkeypatch):
    monkeypatch.setenv(key, "different")
    monkeypatch.setattr(adapter.transport, "invoke", lambda *a, **k: pytest.fail("no model"))
    with pytest.raises(adapter.AutomationHold, match="revision_mismatch"):
        invoke()


@pytest.mark.parametrize("bad", ["[]", "null", "{\"role\":\"judge\",\"role\":\"assessment\"}",
                                "{\"unused\":NaN}", "[" * 3000, " " * 17000])
def test_malformed_duplicate_or_over_budget_context_is_held(bad, mounted):
    _, _, write = mounted
    write(raw=bad)
    with pytest.raises(adapter.AutomationHold, match="context_(unavailable|invalid)"):
        invoke()


def test_context_cannot_choose_credentials_account_or_endpoint(mounted):
    value, _, write = mounted
    value["endpoint"] = "https://alternate.invalid"
    write(value)
    with pytest.raises(adapter.AutomationHold, match="context_invalid"):
        invoke()


@pytest.mark.parametrize("kind", ["symlink", "writable", "missing"])
def test_context_must_be_an_immutable_regular_input(kind, mounted):
    _, path, _ = mounted
    if kind == "symlink":
        target = path.with_name("target.json")
        path.rename(target)
        path.symlink_to(target)
    elif kind == "writable":
        path.chmod(0o644)
    else:
        path.unlink()
    with pytest.raises(adapter.AutomationHold, match="context_unavailable"):
        invoke()


@pytest.mark.parametrize("change", ["role", "sensitivity", "first_party", "client_policy"])
def test_explicit_policy_and_scope_do_not_fall_through(change, mounted, monkeypatch):
    value, _, write = mounted
    monkeypatch.setattr(adapter.transport, "invoke", lambda *a, **k: pytest.fail("no model"))
    kwargs = {}
    if change in ("role", "sensitivity"):
        value["role" if change == "role" else "data_sensitivity"] = "judge" if change == "role" else "public"
        write(value)
    elif change == "first_party":
        kwargs["first_party"] = True
    else:
        monkeypatch.setenv("CLIENT_AI_POLICY", "existing-direct-only-policy")
    with pytest.raises(adapter.AutomationHold, match="scope_mismatch|provider_policy_held"):
        invoke(**kwargs)


def test_unknown_transport_outcome_is_not_replayed(mounted, monkeypatch):
    calls = []
    def failed(*a, **k):
        calls.append(1)
        raise adapter.transport.SubscriptionTextError("subscription_text_transport_unknown")
    monkeypatch.setattr(adapter.transport, "invoke", failed)
    with pytest.raises(adapter.AutomationHold, match="transport_unknown"):
        invoke()
    assert calls == [1]


def test_real_guest_unix_seam_ignores_ambient_legacy_credentials(mounted, monkeypatch, tmp_path):
    # The HTTP peer is a fixture, not an authenticated provider or qualified host.
    requests = []
    response = {"object": "response", "status": "completed", "model": "gpt-6.1-sol", "output": [{
        "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": "complete fixture result"}]}],
        "usage": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19}}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, dict(self.headers), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            raw = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            return

    sock = tmp_path / "broker.sock"
    monkeypatch.setattr(adapter.transport, "SOCKET", str(sock))
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LITELLM_PROXY_URL", "ANTHROPIC_BASE_URL"):
        monkeypatch.setenv(key, "unused-fixture")
    with socketserver.UnixStreamServer(str(sock), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert invoke() == "complete fixture result"
        finally:
            server.shutdown()
            thread.join(timeout=2)
    assert len(requests) == 1
    path, headers, body = requests[0]
    assert path == "/v1/responses" and body["model"] == "gpt-6.1-sol"
    assert body["tools"] == [] and body["input"][0]["content"][0]["text"] == "complete untrusted input"
    assert not {"authorization", "x-api-key"} & {k.lower() for k in headers}


def classifier(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("subscription_classifier_fixture", root / "scripts/run-pr-classifier.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    agent = tmp_path / "pr-classifier.md"
    agent.write_text("---\nmodel: haiku\ndata_sensitivity: internal\nmax_tokens: 1000\n---\nTrusted classifier instructions.")
    monkeypatch.setattr(module, "CLASSIFIER_AGENT", agent)
    monkeypatch.setattr(module, "_SUBSCRIPTION_CONTEXT", adapter.CONTEXT)
    monkeypatch.setattr(module, "_subscription_runtime", lambda: adapter)
    monkeypatch.setattr(module, "_endpoint", lambda *a, **k: pytest.fail("legacy provider must not be resolved"))
    monkeypatch.setattr(module._policy_module, "AUTOMATION_CONTEXT", adapter.CONTEXT)
    monkeypatch.chdir(tmp_path)
    return module


def test_actual_classifier_required_path_has_no_key_guard_or_input_prefix(mounted, monkeypatch, tmp_path):
    module = classifier(monkeypatch, tmp_path)
    diff = tmp_path / "pr.diff"
    # Tail content and full metadata must survive the legacy prefix boundaries.
    content = "diff --git a/sample.py b/sample.py\n--- a/sample.py\n+++ b/sample.py\n@@ -0,0 +1 @@\n+" + "x" * 55000 + "TAIL_SENTINEL\n"
    diff.write_text(content)
    output = tmp_path / "manifest.json"
    body = "b" * 5000 + "BODY_TAIL_SENTINEL"
    calls = []
    def complete(system, user, **kwargs):
        calls.append((system, user, kwargs))
        return {"text": json.dumps({"invoke_skills": ["scope-adherence"], "invoke_agents": [], "skip_skills": [],
                 "depth": "narrow", "reason": "fixture", "door": "two-way",
                 "blast_radius": "small", "danger_reason": "fixture only"})}
    monkeypatch.setattr(adapter.transport, "invoke", complete)
    monkeypatch.setattr(sys, "argv", ["classifier", "--diff", str(diff), "--output", str(output), "--body", body])
    module.main()
    manifest = json.loads(output.read_text())
    assert manifest["invoke_skills"] == ["scope-adherence"]
    assert len(calls) == 1 and "TAIL_SENTINEL" in calls[0][1] and "BODY_TAIL_SENTINEL" in calls[0][1]
    assert "diff truncated" not in calls[0][1]
    assert calls[0][2]["role"] == "assessment"


@pytest.mark.parametrize("outcome", ["unknown", "bad_manifest", "over_budget", "sensitivity", "first_party", "model_pin"])
def test_classifier_failure_never_writes_legacy_fallback_or_replays(outcome, mounted, monkeypatch, tmp_path):
    module = classifier(monkeypatch, tmp_path)
    diff = tmp_path / "pr.diff"
    diff.write_text("diff --git a/sample.py b/sample.py\n+fixture\n")
    output = tmp_path / "manifest.json"
    if outcome == "over_budget":
        diff.write_text("+" + "x" * 180001)
    if outcome == "sensitivity":
        module.CLASSIFIER_AGENT.write_text("---\nmodel: haiku\ndata_sensitivity: restricted\n---\nTrusted fixture")
    if outcome == "first_party":
        module.CLASSIFIER_AGENT.write_text("---\nmodel: haiku\nfirst_party: true\n---\nTrusted fixture")
    if outcome == "model_pin":
        module.CLASSIFIER_AGENT.write_text("---\nmodel: haiku\nclient_policy_model: explicit-other-model\n---\nTrusted fixture")
    calls = []
    def complete(*args, **kwargs):
        calls.append(1)
        if outcome == "unknown":
            raise adapter.transport.SubscriptionTextError("subscription_text_transport_unknown")
        if outcome in {"sensitivity", "over_budget"}:
            # Keep the real guest client's validation for these cases, no HTTP.
            return original(*args, **kwargs)
        return {"text": "not a valid manifest"}
    original = adapter.transport.invoke
    monkeypatch.setattr(adapter.transport, "invoke", complete)
    monkeypatch.setattr(sys, "argv", ["classifier", "--diff", str(diff), "--output", str(output)])
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 1 and not output.exists()
    assert len(calls) <= 1


def guest_loader():
    path = Path(__file__).resolve().parents[1] / "scripts/lib/subscription_guest.py"
    spec = importlib.util.spec_from_file_location("guest_loader_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_guest_source_loader_ignores_package_initializers_and_valid_candidate_bytecode(mounted, monkeypatch, tmp_path):
    _, context, _ = mounted
    loader = guest_loader()
    directory = tmp_path / "ci"
    directory.mkdir()
    marker = tmp_path / "initializer-executed"
    (directory / "__init__.py").write_text(f"open({str(marker)!r}, 'w').write('bad')")
    for leaf in loader.FILES:
        shutil.copyfile(loader.SOURCE / (leaf + ".py"), directory / (leaf + ".py"))
    target = directory / "codex_text_response.py"
    # A real timestamp-valid .pyc would raise if a regular source loader used it.
    malicious = tmp_path / "cached.py"
    body = "raise RuntimeError('candidate-cache-executed')\n"
    malicious.write_text(body + "#" * (target.stat().st_size - len(body)))
    os.utime(malicious, (target.stat().st_atime, target.stat().st_mtime))
    cache = Path(importlib.util.cache_from_source(str(target)))
    cache.parent.mkdir()
    py_compile.compile(str(malicious), cfile=str(cache), dfile=str(target), doraise=True)
    monkeypatch.setattr(loader, "SOURCE", directory)
    monkeypatch.setattr(loader, "CONTEXT", context)
    runtime = loader.runtime()
    assert runtime.SCHEMA == adapter.SCHEMA and not marker.exists()
    assert runtime.transport.text_completion.__module__.startswith("_subscription_guest_")


@pytest.mark.parametrize("field", ["GITHUB_ACTIONS", "GH_TOKEN", "GITHUB_TOKEN", "DOPPLER_TOKEN_PRD",
                                    "GH_AUTOMATION_TOKEN", "ANTHROPIC_API_KEY", "GITHUB_APP_PRIVATE_KEY"])
def test_secret_bearing_host_never_imports_or_calls_guest_runtime(field, mounted, monkeypatch, tmp_path):
    module = classifier(monkeypatch, tmp_path)
    monkeypatch.setenv(field, "true" if field == "GITHUB_ACTIONS" else "private-fixture")
    monkeypatch.setattr(module, "_subscription_runtime", lambda: pytest.fail("must not import runtime"))
    with pytest.raises(RuntimeError, match="isolated_guest_required"):
        module._call_api("system", "user", "haiku", 1000)


def guest_module(name, mounted, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("guest_" + name.replace("-", "_"), root / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_SUBSCRIPTION_CONTEXT", adapter.CONTEXT)
    monkeypatch.setattr(module, "_subscription_runtime", lambda: adapter)
    monkeypatch.setattr(module._policy_module, "AUTOMATION_CONTEXT", adapter.CONTEXT)
    if hasattr(module, "_endpoint"):
        monkeypatch.setattr(module, "_endpoint", lambda *a, **k: pytest.fail("legacy route must not be read"))
    return module


def test_guest_preserves_controller_coordinates_and_mandatory_roster_without_hosted_flags(mounted, monkeypatch):
    value, _, _ = mounted
    module = guest_module("run-specialists.py", mounted, monkeypatch)
    assert "GITHUB_ACTIONS" not in os.environ
    context = module._policy_module.assessment_context()
    assert context == {"version": 1, "repository": value["repository"], "head": value["head_sha"],
                       "base": value["base_sha"], "run": "31", "attempt": "1"}
    assert module._policy_module.selected_review_roles() == set(value["selected_roles"])
    assert module._policy_module.bind_review_candidate({"source": "scope-adherence", "_assessment": "forged"})["_assessment"] == context


@pytest.mark.parametrize("field,bad", [("controller_run_id", True), ("controller_run_attempt", 0),
    ("selected_roles", ["scope-adherence", "scope-adherence"]), ("selected_roles", ["../other"]),
    ("selected_roles", [False])])
def test_missing_or_ambiguous_guest_provenance_is_never_legacy_unbound(field, bad, mounted, monkeypatch):
    value, _, write = mounted
    value[field] = bad
    write(value)
    module = guest_module("run-specialists.py", mounted, monkeypatch)
    with pytest.raises(ValueError, match="context_invalid"):
        module._policy_module.assessment_context()
    with pytest.raises(ValueError):
        module._policy_module.selected_review_roles()


def test_actual_specialist_writes_bound_candidate_without_legacy_key_or_partial_input(mounted, monkeypatch, tmp_path):
    module = guest_module("run-specialists.py", mounted, monkeypatch)
    monkeypatch.setattr(module, "_load_skill", lambda _: ({"model": "haiku", "data_sensitivity": "internal"}, "Trusted specialist fixture"))
    monkeypatch.setenv("PR_BODY", "x" * 13000 + "BODY_TAIL")
    calls = []
    def response(system, user, **kwargs):
        calls.append((system, user, kwargs))
        return {"text": json.dumps({"source": "scope-adherence", "findings": []})}
    monkeypatch.setattr(adapter.transport, "invoke", response)
    name, error = module._invoke_skill("scope-adherence", "diff --git a/sample.py b/sample.py\n+FULL_DIFF", tmp_path)
    assert name == "scope-adherence" and error is None and len(calls) == 1
    assert "BODY_TAIL" in calls[0][1] and "FULL_DIFF" in calls[0][1]
    result = json.loads((tmp_path / "scope-adherence.json").read_text())
    assert result["findings"] == [] and result["_assessment"]["run"] == "31"


@pytest.mark.parametrize("failure", ["invalid_json", "unknown", "protected", "unselected"])
def test_specialist_failures_keep_missing_coverage_and_never_proxy_or_replay(failure, mounted, monkeypatch, tmp_path):
    module = guest_module("run-specialists.py", mounted, monkeypatch)
    monkeypatch.setattr(module, "_load_skill", lambda _: ({"model": "haiku", "first_party": failure == "protected"}, "Trusted fixture"))
    if failure == "protected":
        monkeypatch.setenv("SECURITY_REVIEW_MODE", "proxy")
    if failure == "unselected":
        value, _, write = mounted
        value["selected_roles"] = []
        write(value)
    calls = []
    def response(*a, **k):
        calls.append(1)
        if failure == "unknown":
            raise adapter.transport.SubscriptionTextError("subscription_text_transport_unknown")
        return {"text": "malformed"}
    monkeypatch.setattr(adapter.transport, "invoke", response)
    _, error = module._invoke_skill("scope-adherence", "+fixture", tmp_path)
    result = json.loads((tmp_path / "scope-adherence.json").read_text())
    assert error and result["skipped"] and result["_assessment"]["head"] == "a" * 40
    assert len(calls) <= 1


def test_broad_agent_retains_instructions_findings_and_no_fallback(mounted, monkeypatch):
    module = guest_module("run-broad-agents.py", mounted, monkeypatch)
    monkeypatch.setattr(module, "_proxy_base", lambda: pytest.fail("legacy route must not be read"))
    config = module.AgentConfig("systems-consistency", "sonnet", "internal", "Trusted system", "COMPLETE_CHECKS")
    calls = []
    def response(system, user, **kwargs):
        calls.append((system, user, kwargs))
        return {"text": json.dumps({"findings": [{"file": "sample.py", "line": 1,
                "severity": "WARNING", "message": "fixture finding", "fix": "Inspect `sample.py:1` against its contract."}]})}
    monkeypatch.setattr(adapter.transport, "invoke", response)
    result = module.invoke_agent(config, "", "COMPLETE_DIFF")
    assert result["source"] == "systems-consistency" and result["findings"][0]["line"] == 1
    assert len(calls) == 1 and "COMPLETE_CHECKS" in calls[0][1] and "COMPLETE_DIFF" in calls[0][1]


@pytest.mark.parametrize("failure", ["invalid_json", "unknown", "protected", "unselected"])
def test_broad_agent_failure_is_incomplete_without_legacy_route(failure, mounted, monkeypatch):
    module = guest_module("run-broad-agents.py", mounted, monkeypatch)
    monkeypatch.setattr(module, "_proxy_base", lambda: pytest.fail("legacy route must not be read"))
    config = module.AgentConfig("systems-consistency", "sonnet", "internal", "system", "checks", first_party=failure == "protected")
    if failure == "unselected":
        value, _, write = mounted
        value["selected_roles"] = []
        write(value)
    calls = []
    def response(*a, **k):
        calls.append(1)
        if failure == "unknown":
            raise adapter.transport.SubscriptionTextError("subscription_text_transport_unknown")
        return {"text": "malformed"}
    monkeypatch.setattr(adapter.transport, "invoke", response)
    result = module.invoke_agent(config, "", "diff")
    assert result["skipped"] and len(calls) <= 1


@pytest.fixture
def guest_judge(mounted, monkeypatch, tmp_path):
    module = guest_module("run-judge.py", mounted, monkeypatch)
    root = tmp_path / "source"
    root.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL).decode().strip()
    git("init")
    git("config", "user.email", "offline@example.invalid")
    git("config", "user.name", "Offline fixture")
    (root / "policy.py").write_text("# null intentionally allowed\ndef allowed(origin):\n    return origin == 'null'\n")
    (root / "test_policy.py").write_text("def test_denial():\n    assert allowed('tenant-b') is False\n")
    git("add", ".")
    git("commit", "-m", "Synthetic source")
    head = git("rev-parse", "HEAD")
    value, _, write = mounted
    value.update(role="judge", head_sha=head, base_sha=head)
    write(value)
    monkeypatch.setenv("PR_HEAD_SHA", head)
    monkeypatch.setenv("PR_BASE_SHA", head)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(root))
    monkeypatch.delenv("PR_ASSESSMENT_EVIDENCE_ROOT", raising=False)
    monkeypatch.setattr(module, "_urlopen_https", lambda *a, **k: pytest.fail("no HTTP fallback"))
    finding = module.Finding("systems-consistency", "policy.py", 3, "ERROR",
        "Missing tenant-denial test", "Add tenant-b denial assertion in test_policy.py:2")
    answer = {"version": 2, "decisions": [{"finding_id": finding.finding_id, "decision": "keep",
        "gate": "passed", "reason": "Exact source evidence supports this disposition",
        "evidence_ids": [hashlib.sha256(b"policy.py|3").hexdigest()[:16]]}], "investigations": []}
    return module.Judge("unused", 7, head), finding, answer


def test_guest_judge_uses_actual_immutable_evidence_and_existing_decision_parser(guest_judge, monkeypatch):
    judge, finding, answer = guest_judge
    calls = []
    def response(system, user, **kwargs):
        calls.append((system, user, kwargs))
        return {"text": json.dumps(answer)}
    monkeypatch.setattr(adapter.transport, "invoke", response)
    result = judge.invoke_judge_agent([finding])
    assert result["complete"] and result["surviving"][0]["finding_id"] == finding.finding_id
    assert len(calls) == 1 and calls[0][2]["role"] == "judge"
    assert "null intentionally allowed" in calls[0][1]
    assert result["review_action"] == "REQUEST_CHANGES"


@pytest.mark.parametrize("failure", ["unknown", "invalid_json", "protected", "invalid_evidence"])
def test_guest_judge_never_replays_or_converts_missing_evidence_to_clean(failure, guest_judge, monkeypatch):
    judge, finding, answer = guest_judge
    if failure == "protected":
        judge._has_first_party_candidates = True
    if failure == "invalid_evidence":
        answer["decisions"][0]["evidence_ids"] = ["unavailable"]
    calls = []
    def response(*a, **k):
        calls.append(1)
        if failure == "unknown":
            raise adapter.transport.SubscriptionTextError("subscription_text_transport_unknown")
        return {"text": "malformed" if failure == "invalid_json" else json.dumps(answer)}
    monkeypatch.setattr(adapter.transport, "invoke", response)
    result = judge.invoke_judge_agent([finding])
    assert result is None or not result["complete"]
    assert len(calls) <= 1


@pytest.mark.parametrize(("skill", "frontmatter"), [
    ("scope-adherence", {"required_security_review": True}),
    ("scope-adherence", {"first_party": True}),
    ("security-boundary-test", {}),
])
def test_governed_default_security_specialist_is_held_not_run_on_subscription(skill, frontmatter, mounted, monkeypatch, tmp_path):
    """Governed mode clears first_party; the fixed route must still hold security roles."""
    module = guest_module("run-specialists.py", mounted, monkeypatch)
    monkeypatch.delenv("SECURITY_REVIEW_MODE", raising=False)
    value, _, write = mounted
    value["selected_roles"] = [skill]
    write(value)
    monkeypatch.setattr(module, "_load_skill", lambda _: ({"model": "haiku", "data_sensitivity": "internal", **frontmatter}, "Trusted fixture"))
    monkeypatch.setattr(adapter.transport, "invoke", lambda *a, **k: pytest.fail("security role reached subscription transport"))
    _, error = module._invoke_skill(skill, "+fixture", tmp_path)
    result = json.loads((tmp_path / f"{skill}.json").read_text())
    assert error and result["skipped"] == "subscription_explicit_provider_policy_held"
    assert result["first_party"] is True and result["findings"] == []


@pytest.mark.parametrize("flag", ["first_party", "required_security_review"])
def test_governed_default_security_broad_agent_is_held_as_missing_coverage(flag, mounted, monkeypatch):
    module = guest_module("run-broad-agents.py", mounted, monkeypatch)
    monkeypatch.delenv("SECURITY_REVIEW_MODE", raising=False)
    monkeypatch.setattr(module, "_proxy_base", lambda: pytest.fail("legacy route must not be read"))
    monkeypatch.setattr(adapter.transport, "invoke", lambda *a, **k: pytest.fail("security role reached subscription transport"))
    config = module.AgentConfig("security-deep-dive", "sonnet", "internal", "system", "checks", **{flag: True})
    result = module.invoke_agent(config, "", "diff")
    assert result == {"source": "security-deep-dive", "findings": [], "first_party": True,
                      "skipped": "subscription_explicit_provider_policy_held"}
