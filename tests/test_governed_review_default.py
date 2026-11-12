"""Exercise actual review entrypoints without vendor credentials or live HTTP."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location("governed_" + name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def configured_router(monkeypatch):
    for name in ("SECURITY_REVIEW_MODE", "CLIENT_AI_POLICY", "GITHUB_ACTIONS",
                 "LLM_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_DIRECT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LITELLM_PROXY_URL", "https://router.example")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "offline-router-token")


@pytest.mark.parametrize("runner", ["run-pr-classifier.py", "run-specialists.py", "run-judge.py"])
def test_actual_endpoints_default_to_router_even_for_legacy_direct_flags(runner, monkeypatch):
    module = load(runner)
    monkeypatch.setattr(module, "require_direct_llm_admission", lambda **_: pytest.fail("direct admission attempted"))
    args = {} if runner == "run-pr-classifier.py" else {"direct": True}
    assert module._endpoint(**args) == ("offline-router-token", "https://router.example/v1/messages", True)


@pytest.mark.parametrize("runner", ["run-pr-classifier.py", "run-specialists.py", "run-judge.py"])
def test_default_missing_router_never_uses_ambient_vendor_key(runner, monkeypatch):
    module = load(runner)
    monkeypatch.delenv("LITELLM_PROXY_URL")
    monkeypatch.setenv("ANTHROPIC_DIRECT_API_KEY", "offline-direct-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-ambient-token")
    assert module._endpoint()[0] == ""


@pytest.mark.parametrize("origin", [
    "https://api.anthropic.com", "https://api.openai.com", "http://router.example",
    "https://user:password@router.example", "https://router.example?key=unsafe",
])
def test_default_does_not_mistake_provider_or_invalid_origin_for_router(origin, monkeypatch):
    module = load("run-specialists.py")
    monkeypatch.setenv("LITELLM_PROXY_URL", origin)
    assert module._endpoint() == ("", "", True)


def test_legacy_restrictive_client_policy_remains_a_hold_without_egress_permission(monkeypatch):
    module = load("run-specialists.py")
    monkeypatch.setenv("CLIENT_AI_POLICY", "restricted-engagement")
    with pytest.raises(module.RouteAdmissionError, match="compatible declared route"):
        module._endpoint(direct=True)


def test_security_broad_request_uses_actual_router_and_honest_provenance(monkeypatch):
    module = load("run-broad-agents.py")
    calls = []

    def transport(request, **_):
        calls.append((request.full_url, json.loads(request.data), dict(request.header_items())))
        return io.BytesIO(json.dumps({"content": [{"type": "text", "text":
            '{"source":"security-deep-dive","findings":[]}'}], "stop_reason": "end_turn"}).encode())

    monkeypatch.setattr(module, "_urlopen_https", transport)
    monkeypatch.setattr(module, "require_direct_llm_admission", lambda **_: pytest.fail("direct admission attempted"))
    agent = module.AgentConfig("security-deep-dive", "sonnet", "internal", "Review", "Return JSON",
                               client_policy_model="claude-sonnet-4-6", first_party=True)
    result = module.invoke_agent(agent, "", "complete diff")
    assert result["findings"] == [] and not result["first_party"]
    assert len(calls) == 1
    url, payload, headers = calls[0]
    assert url == "https://router.example/v1/messages"
    assert payload["model"] == "sonnet" and payload["max_tokens"] == 2400
    assert headers["Authorization"] == "Bearer offline-router-token"


def test_explicit_external_direct_mode_preserves_actual_shipped_security_role(monkeypatch):
    module = load("run-broad-agents.py")
    monkeypatch.setenv("SECURITY_REVIEW_MODE", "direct")
    monkeypatch.setenv("ANTHROPIC_DIRECT_API_KEY", "offline-explicit-direct-token")
    admitted, calls = [], []
    monkeypatch.setattr(module, "require_direct_llm_admission", lambda **kw: admitted.append(kw))
    def transport(request, **_):
        calls.append(request.full_url)
        return io.BytesIO(json.dumps({"content": [{"type": "text", "text":
            json.dumps({"source": "security-deep-dive", "findings": [
                {"file": "flow.py", "line": 2, "severity": "WARNING", "message": "Review this flow"}
            ]})}], "stop_reason": "end_turn"}).encode())
    monkeypatch.setattr(module, "_urlopen_https", transport)
    agent = module.parse_agent_file(ROOT / ".claude/agents/security-deep-dive.md")
    result = module.invoke_agent(agent, "offline-router-token", "complete diff")
    assert admitted == [{"role": "security-deep-dive"}]
    assert calls == ["https://api.anthropic.com/v1/messages"]
    assert result["first_party"] is True


def test_specialist_cannot_self_assert_first_party_provenance(tmp_path, monkeypatch):
    module = load("run-specialists.py")
    monkeypatch.setattr(module, "_load_skill", lambda _: ({"model": "haiku", "first_party": True}, "Review"))
    monkeypatch.setattr(module, "_call_api", lambda *_, **__: json.dumps(
        {"source": "shell-security", "findings": [], "first_party": True}))
    _, error = module._invoke_skill("shell-security", "complete diff", tmp_path)
    assert error is None
    result = json.loads((tmp_path / "shell-security.json").read_text())
    assert result["first_party"] is False


def test_default_preserves_governance_no_ai_without_any_model_call(tmp_path, monkeypatch):
    module = load("run-specialists.py")
    monkeypatch.setattr(module, "_load_skill", lambda _: ({"model": "haiku", "data_sensitivity": "restricted"}, "Review"))
    monkeypatch.setattr(module, "_call_api", lambda *_, **__: pytest.fail("restricted input dispatched"))
    module._invoke_skill("shell-security", "restricted diff", tmp_path)
    assert json.loads((tmp_path / "shell-security.json").read_text())["skipped"] == "restricted_no_ai"


def test_explicit_estate_qualification_mode_still_exists(monkeypatch):
    module = load("lib/protected_review.py")
    assert module.enabled() is False
    monkeypatch.setenv("SECURITY_REVIEW_MODE", "estate-ci-v1")
    assert module.enabled() is True


@pytest.mark.parametrize("name", ["pr-assessment-reusable.yml", "pr-autofix-loop-reusable.yml"])
def test_reusable_workflow_installs_governed_proxy_by_default(name):
    import yaml
    workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
    # YAML 1.1 treats the GitHub `on` key as True.
    call = workflow[True]["workflow_call"]
    assert call["inputs"]["provider_mode"]["default"] == "proxy"
    assert call["secrets"]["ANTHROPIC_API_KEY"]["required"] is False
    if name == "pr-assessment-reusable.yml":
        assert "SECURITY_REVIEW_MODE" not in workflow["env"]
    else:
        # The default selects the already-supported proxy transport and model
        # resolver without changing action authority, success cap or jobs.
        job = workflow["jobs"]["autofix"]
        assert "inputs.litellm_proxy_url" in job["env"]["ANTHROPIC_BASE_URL"]
        assert "secrets.LITELLM_MASTER_KEY" in job["env"]["ANTHROPIC_API_KEY"]
        model = next(step for step in job["steps"] if step.get("id") == "model")
        assert "tier-2-agentic-pro" in model["run"]
        assert call["inputs"]["max_successful_fixes"]["default"] == "1"


@pytest.mark.parametrize("name", ["pr-assessment.yml", "pr-assessment-reusable.yml"])
def test_trusted_runtime_owns_mode_default_across_bootstrap_versions(name):
    import yaml
    workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
    # PR jobs restore immutable base runtime; reusable hydration may select an
    # older pack_ref. Never send a new enum value into that older trusted code.
    assert "SECURITY_REVIEW_MODE" not in workflow.get("env", {})
    for job in workflow["jobs"].values():
        assert "SECURITY_REVIEW_MODE" not in job.get("env", {})
        for step in job.get("steps", []):
            assert "SECURITY_REVIEW_MODE" not in step.get("env", {})
    # After merge/hydration, the shipped runtime itself selects governed review
    # before any legacy direct hint or credential can be considered.
    assert load("lib/agent_capabilities.py").review_mode({}) == "governed"


@pytest.mark.parametrize("state", ["complete", "file_level", "bandit_path", "missing_sast", "sensitive_source"])
def test_actual_security_batch_binds_sast_and_safe_git_source(tmp_path, monkeypatch, state):
    module = load("run-broad-agents.py")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()
    git("init", "-q")
    path = tmp_path / "flow.py"
    path.write_text("def checked(value):\n    return value\n")
    git("add", "flow.py")
    git("-c", "user.email=offline@example.invalid", "-c", "user.name=Offline", "commit", "-qm", "Base")
    base = git("rev-parse", "HEAD")
    text = "def checked(value):\n    return bool(value)\n"
    if state == "sensitive_source":
        text += "password = " + repr("fixture-literal-value") + "\n"
    path.write_text(text)
    git("add", "flow.py")
    git("-c", "user.email=offline@example.invalid", "-c", "user.name=Offline", "commit", "-qm", "Candidate")
    head = git("rev-parse", "HEAD")
    monkeypatch.setenv("PR_HEAD_SHA", head)
    monkeypatch.setenv("PR_BASE_SHA", base)
    diff = git("diff", "--full-index", base, head)
    # Worktree content is not review source, including at a reported location.
    path.write_text("raise RuntimeError('DIRTY_WORKTREE_POISON')\n")
    sast = [{"file": "flow.py", "line": 2, "rule": "fixture", "message": "SAST_TAIL"}]
    if state == "file_level":
        sast[0]["line"] = None
    if state == "bandit_path":
        sast[0]["file"] = "./flow.py"
    if state != "missing_sast":
        (tmp_path / ".ai").mkdir()
        (tmp_path / ".ai/sast-findings.json").write_text(json.dumps(sast))
    manifest, source = tmp_path / "manifest.json", tmp_path / "input.diff"
    manifest.write_text(json.dumps({"depth": "broad", "invoke_agents": ["security-deep-dive"]}))
    source.write_text(diff)
    calls = []
    def transport(request, **_):
        calls.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"content": [{"type": "text", "text":
            '{"source":"security-deep-dive","findings":[]}'}], "stop_reason": "end_turn"}).encode())
    monkeypatch.setattr(module, "_urlopen_https", transport)
    output = tmp_path / "candidates"
    assert module.run_broad_agents(manifest, source, output, ROOT / ".claude/agents") == 0
    result = json.loads((output / "security-deep-dive.json").read_text())
    if state not in {"complete", "file_level", "bandit_path"}:
        assert not calls and result["skipped"] == "source_context_unavailable"
    else:
        assert len(calls) == 1 and result["findings"] == [] and not result["first_party"]
        user = calls[0]["messages"][0]["content"]
        assert diff in user and "SAST_TAIL" in user and head in user
        assert "return bool(value)" in user and "DIRTY_WORKTREE_POISON" not in user
        assert user.index("<review_source_context>") < user.index("</untrusted_diff>")
