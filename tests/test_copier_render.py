"""Test suite for Copier template rendering (copier.yml)."""
import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(
    not (ROOT / "copier.yml").exists(),
    reason="template source absent (rendered instance)"
)

# Feature flags control optional surfaces through conditional copier `_exclude`
# entries. Literal Jinja filenames remain forbidden because Copier used to copy
# them as invalid brace-containing paths.
FEATURE_FLAGS = (
    "oss_routing",
    "browserbase",
    "codex_adversarial",
    "kl_integration",
    "langfuse_telemetry",
    "mesh_telemetry",
)


def render(dst, **data):
    """Render template to dst directory. Bool values as 'true'/'false' strings."""
    cmd = [
        sys.executable,
        "-m",
        "copier",
        "copy",
        "--defaults",
        "--quiet",
        "--vcs-ref=HEAD",
    ]
    for k, v in data.items():
        cmd.append(f"--data={k}={v}")
    cmd.extend([str(ROOT), str(dst)])
    subprocess.run(cmd, check=True)


def file_set(d):
    """Return set of relative POSIX paths under d, excluding .copier-answers.yml."""
    result = set()
    for p in Path(d).rglob("*"):
        if p.is_file():
            relpath = p.relative_to(d)
            if relpath.name != ".copier-answers.yml":
                result.add(relpath.as_posix())
    return result


def file_sha(p):
    """Compute SHA256 of file."""
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


@pytest.fixture(scope="session")
def default_render(tmp_path_factory):
    """Render unbranded default template (cached across test session)."""
    dst = tmp_path_factory.mktemp("default")
    render(dst)
    return dst


@pytest.fixture(scope="session")
def branded_render(tmp_path_factory):
    """Render branded template (cached across test session)."""
    dst = tmp_path_factory.mktemp("branded")
    render(dst, install_mode="branded")
    return dst


# Section §8-N2: Blind-sed corruption defect-class lint.
# Post-render validation: assert no doubled-backslash escapes, restricted tier leaks, or OSS-only markers.
DOUBLED_BACKSLASH_ESCAPE_RE = re.compile(r"\\\\[nrt]")


def _template_source_for(relpath):
    """Template file a rendered path came from, or None if it is generated.

    Copier renders `x` from `x` or from `x.jinja`; anything else (e.g.
    .copier-answers.yml) has no source counterpart.
    """
    direct = ROOT / relpath
    if direct.is_file():
        return direct
    jinja = ROOT / f"{relpath}.jinja"
    return jinja if jinja.is_file() else None


def _escape_occurrences(text):
    """Every doubled-backslash escape with its surrounding context, in order.

    Context, not a count. A bare count is blind to three real corruptions the
    §8-N2 defect class covers: the render deleting one escape while adding
    another elsewhere (net zero), rewriting `\\n` into `\\t` in place, and
    moving an escape to a different line. Each leaves the total unchanged.
    """
    return [
        (m.group(0), text[max(0, m.start() - 40):m.end() + 40])
        for m in DOUBLED_BACKSLASH_ESCAPE_RE.finditer(text)
    ]


def test_no_render_corruption(default_render):
    """Post-render corruption lint: verify no blind-sed artifacts remain.

    Covers §8-N2 defect class: doubled backslash escapes, tier-mixing, OSS-only markers.

    The doubled-backslash check is RENDER-RELATIVE, not absolute. The defect
    class is "rendering mangled a backslash", so the test asserts the rendered
    file's escapes are IDENTICAL — same sequences, same surrounding context, same
    order — to its template source's. An absolute scan cannot express that:
    `\\n` is legitimate, unremarkable content in ordinary source files — a
    docstring quoting a Python string literal (tests/test_run_judge_review_lookup.py)
    and a JSON fixture holding an escaped newline (tests/test_tier_reusables.py)
    both carry one, and both were flagged as "corruption" while their rendered
    and source escapes were identical. That false positive made this test
    permanently red on main, and a permanently red lint is a disabled lint.

    Comparing occurrences rather than totals is deliberate: `rendered <= source`
    passes a render that swaps one escape for another, which is exactly the
    corruption this guard claims to detect. Files with no template source
    (generated output) keep the strict zero-tolerance check.
    """
    for p in default_render.rglob("*"):
        if not p.is_file():
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        assert "(not OSS)" not in text, f"{p} contains '(not OSS)' marker"
        assert "restricted/restricted" not in text, f"{p} contains tier-mixing 'restricted/restricted'"

        rendered_escapes = _escape_occurrences(text)
        source = _template_source_for(p.relative_to(default_render))
        source_escapes = (
            _escape_occurrences(source.read_text(errors="ignore")) if source else []
        )
        # No early return on an empty RENDERED list. The invariant is equality,
        # so a render that deletes or collapses the source's only escape is a
        # violation too — and skipping straight past it was exactly the case an
        # `if not rendered_escapes: continue` waved through.
        if not rendered_escapes and not source_escapes:
            continue
        assert rendered_escapes == source_escapes, (
            f"{p} doubled-backslash escapes differ from the template source "
            f"(rendered={[e for e, _ in rendered_escapes]}, "
            f"source={[e for e, _ in source_escapes]}); "
            f"source={source or 'GENERATED (none permitted)'}"
        )


def test_default_render_clean(default_render):
    """Verify rendered output contains no unrendered placeholders or scaffolding."""
    # Check for unrendered Jinja markers
    for p in default_render.rglob("*"):
        if not p.is_file():
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            # Unreadable file (permissions/special) — content scan does not apply.
            continue
        assert "{{ATTRIBUTION_LINE}}" not in text, f"{p.name} has unrendered {{{{ATTRIBUTION_LINE}}}}"

    # Check paths don't contain Jinja conditionals or .jinja suffix
    for p in file_set(default_render):
        assert "{% if" not in p, f"Path contains unrendered Jinja: {p}"
        assert not p.endswith(".jinja"), f"Path ends with .jinja suffix: {p}"

    # Check internal scaffolding absent
    scaffolding = {
        "pack.manifest.yml",
        ".brand",
        "scripts/render-pack.py",
        "copier.yml",
        ".copier-answers.yml.jinja",
    }
    for scaffold in scaffolding:
        assert not (default_render / scaffold).exists(), f"Scaffolding leaked: {scaffold}"

    # Check .copier-answers.yml IS present
    assert (default_render / ".copier-answers.yml").exists(), ".copier-answers.yml missing"


def test_email_capture_framework_renders(default_render):
    """The portable executable, contracts, and CI interface ship together."""
    expected = {
        "bin/email-capture",
        "email_capture/core.py",
        "email_capture/cli.py",
        "schemas/email-capture/profile.schema.json",
        "schemas/email-capture/normalized-message.schema.json",
        ".github/workflows/email-capture-conformance.yml",
        ".github/workflows/email-capture-live-conformance.yml",
        ".claude/commands/email-capture.md",
        ".codex/commands/email-capture.md",
        ".cursor/commands/email-capture.md",
    }
    missing = expected - file_set(default_render)
    assert not missing, f"rendered email-capture framework is incomplete: {sorted(missing)}"
    assert (default_render / "bin/email-capture").stat().st_mode & 0o111


def test_feature_flags_gate_optional_surfaces(default_render):
    """Disabled flags omit optional files; enabling all flags restores them."""
    with tempfile.TemporaryDirectory() as tmpdir:
        flags_dst = Path(tmpdir) / "all-flags"
        flags_dst.mkdir()
        render(flags_dst, **{flag: "true" for flag in FEATURE_FLAGS})
        flags_files = file_set(flags_dst)
        default_files = file_set(default_render)

        optional = {
            "docs/us-oss-eligibility-matrix.md",
            ".claude/skills/om-fact-capture/SKILL.md",
            ".claude/skills/om-readiness/SKILL.md",
            ".claude/skills/om-staff-answer/SKILL.md",
            ".claude/skills/om-handover/SKILL.md",
            "docs/knowledge-layer-access.md",
            ".claude/commands/browse.md",
            ".claude/commands/nav-record.md",
            ".claude/commands/nav-replay.md",
            ".claude/agents/codex-adversarial.md",
            "mesh-contract.yaml.template",
        }
        assert not (optional & default_files), "default render leaked disabled features"
        assert optional <= flags_files, "all-flags render omitted enabled features"
        assert flags_files - default_files == optional


def test_pack_terminal_fixture_suite_is_not_rendered(default_render):
    """Producer parity fixtures need artifact trees that consumers do not ship."""
    assert not (default_render / "tests/test_session_terminal_callers.py").exists()
    assert (default_render / "scripts/session-unfinished-work-check.py").read_bytes() == (
        ROOT / "scripts/session-unfinished-work-check.py").read_bytes()


def test_cost_profile_lean_omits_measured_waste_callers(default_render):
    """cost_profile=lean drops the two callers measured as pure spend (L-3/L-7)
    while keeping the rest of the CI surface byte-identical to standard."""
    lean_only = {
        ".github/workflows/monitor-litellm.yml",
    }
    default_files = file_set(default_render)
    assert lean_only <= default_files, "standard profile must still ship the caller"
    assert ".github/workflows/mutation-testing-diff.yml" not in default_files, \
        "mutation-testing-diff was retired estate-wide and must not ship"
    with tempfile.TemporaryDirectory() as tmpdir:
        dst = Path(tmpdir) / "lean"
        dst.mkdir()
        render(dst, cost_profile="lean")
        lean_files = file_set(dst)
        assert not (lean_only & lean_files), "lean render leaked excluded callers"
        assert default_files - lean_files == lean_only


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ({}, {"Hooks": 5, "Commands": 51, "Skills": 36, "Agents": 27,
              "Scripts": 56, "Husky": 3, "CI": 36, "Docs": 33}),
        ({flag: "true" for flag in FEATURE_FLAGS},
         {"Hooks": 5, "Commands": 54, "Skills": 40, "Agents": 28,
              "Scripts": 56, "Husky": 3, "CI": 36, "Docs": 35}),
        ({"cost_profile": "lean"},
         {"Hooks": 5, "Commands": 51, "Skills": 36, "Agents": 27,
              "Scripts": 56, "Husky": 3, "CI": 35, "Docs": 33}),
        ({"codex_review": "false"},
         {"Hooks": 5, "Commands": 51, "Skills": 36, "Agents": 27,
              "Scripts": 56, "Husky": 3, "CI": 34, "Docs": 33}),
        ({"isolation_profile": "strict"},
         {"Hooks": 5, "Commands": 51, "Skills": 36, "Agents": 27,
              "Scripts": 53, "Husky": 3, "CI": 30, "Docs": 33}),
        ({"isolation_profile": "strict", "cost_profile": "lean"},
         {"Hooks": 5, "Commands": 51, "Skills": 36, "Agents": 27,
              "Scripts": 53, "Husky": 3, "CI": 30, "Docs": 33}),
    ],
)
def test_rendered_readme_counts_match_rendered_tree(flags, expected):
    """Every advertised component count describes the selected render."""
    with tempfile.TemporaryDirectory() as tmpdir:
        dst = Path(tmpdir) / "render"
        dst.mkdir()
        render(dst, **flags)
        readme = (dst / "README-STARTER-PACK.md").read_text(encoding="utf-8")
        locations = {
            "Hooks": (".claude/hooks", "files"),
            "Commands": (".claude/commands", "files"),
            "Skills": (".claude/skills", "dirs"),
            "Agents": (".claude/agents", "files"),
            "Scripts": ("scripts", "files"),
            "Husky": (".husky", "files"),
            "CI": (".github/workflows", "files"),
            "Docs": ("docs", "files"),
        }
        for label, count in expected.items():
            directory, unit = locations[label]
            entries = list((dst / directory).iterdir())
            actual = sum(p.is_dir() if unit == "dirs" else p.is_file() for p in entries)
            assert actual == count, f"test expectation drift for {label}"
            assert re.search(rf"\*\*{label}\*\*.*\({actual} {unit}\)", readme)


def test_validation_layout_distribution_boundary(default_render, branded_render):
    """Ship the opt-in installer/guide, without its Pack-only synthetic suite."""
    for rendered in (default_render, branded_render):
        for rel in ("scripts/install-agent-validation.py", "docs/agent-validation-layout.md"):
            assert (rendered / rel).read_bytes() == (ROOT / rel).read_bytes()
        assert not (rendered / "scripts/tests/test_agent_validation_layout.py").exists()
        # The existing default validation bundle remains available to callers.
        assert (rendered / "scripts/tests/test_agent_check.py").is_file()


def test_brand_answers_match_overlays(branded_render, default_render):
    """Load .copier-answers.yml and verify brand fields match .brand/*.yml."""
    for render_dir, mode in [(branded_render, "branded"), (default_render, "unbranded")]:
        answers_file = render_dir / ".copier-answers.yml"
        answers = yaml.safe_load(answers_file.read_text())

        overlay = yaml.safe_load((ROOT / ".brand" / f"{mode}.yml").read_text())
        brand_values = overlay.get("brand", {})

        for key in ("ecosystem_name", "attribution_line", "org_name"):
            expected = brand_values.get(key, "")
            actual = answers.get(key, "")
            assert actual == expected, (
                f"{mode}: {key} mismatch. "
                f"Expected '{expected}', got '{actual}'"
            )


def test_branded_attribution(branded_render, default_render):
    """Verify branded canary.md has attribution; default does not."""
    branded_canary = branded_render / ".claude" / "commands" / "canary.md"
    assert branded_canary.exists(), "branded canary.md missing"
    branded_text = branded_canary.read_text()
    assert "Based on proven patterns from the Manolii ecosystem." in branded_text, (
        "branded canary.md missing expected attribution"
    )

    default_canary = default_render / ".claude" / "commands" / "canary.md"
    assert default_canary.exists(), "default canary.md missing"
    default_text = default_canary.read_text()
    assert "Manolii" not in default_text, "default canary.md contains 'Manolii'"
    assert "{{" not in default_text, "default canary.md contains unrendered Jinja"


@pytest.mark.parametrize("base_ref", ["origin/trunk", "master"])
def test_agent_validation_base_uses_owner_ref(tmp_path, default_render, base_ref):
    """A real rendered CLI plans correctly with remote or local owner refs."""
    canonical = json.loads((ROOT / "config/agent-checks.json").read_text())
    assert json.loads((default_render / "config/agent-checks.json").read_text()) == canonical
    dst = tmp_path / "custom-integration"
    render(dst, agent_validation_base=base_ref)
    assert json.loads((dst / "config/agent-checks.json").read_text()) == {**canonical, "base": base_ref}
    assert not (dst / "config/agent-checks.json.jinja").exists()
    env = runpy.run_path(str(ROOT / "scripts/agent-check.py"))["git_environment"]()
    branch = base_ref.removeprefix("origin/")
    for args in (("init", "-q", "--initial-branch=" + branch),
                 ("config", "user.email", "fixture@example.invalid"),
                 ("config", "user.name", "Fixture"), ("add", "."),
                 ("commit", "-qm", "rendered integration fixture")):
        subprocess.run(["git", *args], cwd=dst, env=env, check=True, capture_output=True)
    expected = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=dst, env=env, text=True).strip()
    if base_ref.startswith("origin/"):
        subprocess.run(["git", "update-ref", "refs/remotes/" + base_ref, "HEAD"],
                       cwd=dst, env=env, check=True)
    else:
        assert not subprocess.check_output(["git", "remote"], cwd=dst, env=env).strip()
    policy = dst / "docs/agent-validation.md"
    policy.write_text(policy.read_text() + "\nFixture policy change.\n")
    result = subprocess.run([sys.executable, "scripts/agent-check.py", "--plan"],
                            cwd=dst, env=env, check=True, capture_output=True, text=True)
    plan = json.loads(result.stdout)
    assert plan["base"] == expected
    assert plan["paths"] == ["docs/agent-validation.md"]
    policy_check = next(c for c in canonical["checks"] if c["id"] == "agent-validation-contract")
    assert [c["commands"] for c in plan["checks"]] == [policy_check["commands"]]


def test_skip_if_exists_contract():
    """Verify _skip_if_exists in copier.yml and behavioural guarantee."""
    copier_config = yaml.safe_load((ROOT / "copier.yml").read_text())
    skip_if_exists = set(copier_config.get("_skip_if_exists", []))

    expected = {"CLAUDE.md", ".claude/model-routing.json", ".claude/mcp.json", ".claude/hooks/session-start.sh", "ai-manifest.yaml", "config/agent-checks.json",
                # QA framework owner config + host hook files (docs/qa-framework.md)
                "config/qa.json", ".cursor/hooks.json", ".codex/hooks.json",
                ".githooks/pre-push"}
    assert skip_if_exists == expected, (
        f"_skip_if_exists mismatch.\n"
        f"Expected: {expected}\n"
        f"Got: {skip_if_exists}"
    )

    # Behavioural test: local file survives overwrite
    with tempfile.TemporaryDirectory() as tmpdir:
        dst = Path(tmpdir)
        render(dst)

        # Write sentinel to a skip_if_exists file
        claude_file = dst / "CLAUDE.md"
        claude_file.write_text("LOCAL\n")
        session_hook = dst / ".claude/hooks/session-start.sh"
        session_hook.write_text("#!/bin/sh\necho LOCAL\n")
        recipes = dst / "config/agent-checks.json"
        recipes.write_text('{"owner": "local"}\n')
        settings = dst / ".claude/settings.json"
        settings.write_text("{}\n")

        # Re-render with --overwrite
        cmd = [
            sys.executable,
            "-m",
            "copier",
            "copy",
            "--defaults",
            "--quiet",
            "--overwrite",
            "--vcs-ref=HEAD",
            "--data=agent_validation_base=origin/trunk",
            str(ROOT),
            str(dst),
        ]
        subprocess.run(cmd, check=True)

        # Sentinel should survive
        assert claude_file.read_text() == "LOCAL\n"
        assert session_hook.read_text() == "#!/bin/sh\necho LOCAL\n"
        assert recipes.read_text() == '{"owner": "local"}\n'
        # The lifecycle migration is deliberately outside the instance-owned
        # hook, so an update still wires cleanup for existing consumers.
        assert "guard_check.py --clear-session-unfreezes" in settings.read_text()
        assert claude_file.read_text() == "LOCAL\n", "CLAUDE.md was overwritten"

        # But other files should be updated
        agents_file = dst / ".claude" / "agents" / "architecture-impact.md"
        assert agents_file.exists(), "AGENTS.md-like file missing after overwrite"


def test_exec_bits_preserved(default_render):
    """All executable template files keep their executable bit in rendered output."""
    skip_parts = {".git", "__pycache__", "releases", "dist", ".brand"}
    not_shipped = {"scripts/render-pack.py", "tests/test_copier_render.py", "copier.yml"}
    checked = 0
    for src in ROOT.rglob("*"):
        if not src.is_file() or not os.access(src, os.X_OK):
            continue
        rel = src.relative_to(ROOT)
        if any(part in skip_parts for part in rel.parts):
            continue
        if rel.as_posix() in not_shipped:
            continue
        rendered = default_render / rel
        if not rendered.exists():
            continue  # flag-gated file disabled in the default render
        assert os.access(rendered, os.X_OK), f"{rel} lost its executable bit"
        checked += 1
    # Non-vacuity floor: hooks, githooks, husky and setup scripts are executable
    assert checked >= 5, f"only {checked} executable files verified — test went vacuous"


def test_wrapper_matches_direct_copier():
    """Render via render-pack.py wrapper and compare to direct copier.copy."""
    wrapper_script = ROOT / "scripts" / "render-pack.py"
    if not wrapper_script.exists():
        pytest.skip("render-pack.py missing")

    with tempfile.TemporaryDirectory() as tmpdir:
        direct_dst = Path(tmpdir) / "direct"
        direct_dst.mkdir()
        render(direct_dst)
        direct_files = file_set(direct_dst)

        # Build direct file → sha256 map
        direct_map = {}
        for relpath in direct_files:
            fpath = direct_dst / relpath
            direct_map[relpath] = file_sha(fpath)

        # Render via wrapper
        wrapper_dst = Path(tmpdir) / "wrapper"
        wrapper_dst.mkdir()
        cmd = [
            sys.executable,
            str(wrapper_script),
            "--mode",
            "unbranded",
            "--output",
            str(wrapper_dst),
            "--force",
        ]
        subprocess.run(cmd, check=True)
        wrapper_files = file_set(wrapper_dst)

        # Build wrapper file → sha256 map
        wrapper_map = {}
        for relpath in wrapper_files:
            fpath = wrapper_dst / relpath
            wrapper_map[relpath] = file_sha(fpath)

        # Compare
        assert direct_files == wrapper_files, (
            f"File set mismatch.\n"
            f"Only in direct: {direct_files - wrapper_files}\n"
            f"Only in wrapper: {wrapper_files - direct_files}"
        )

        assert direct_map == wrapper_map, (
            f"File content mismatch (SHA256).\n"
            f"Mismatched files: "
            f"{sorted([p for p in direct_files if direct_map.get(p) != wrapper_map.get(p)])}"
        )


def test_pack_components_contract(default_render):
    """Verify pack-components.yml schema and default instance contract."""
    pack_file = default_render / "pack-components.yml"
    assert pack_file.exists(), "pack-components.yml missing"

    data = yaml.safe_load(pack_file.read_text())

    # Schema version
    assert data.get("schema_version") == 1, f"schema_version: expected 1, got {data.get('schema_version')}"

    # Instance fields
    instance = data.get("instance", {})
    assert instance.get("name") == "your-instance", f"instance.name: expected 'your-instance', got '{instance.get('name')}'"
    assert instance.get("doppler_config") == "prd", f"instance.doppler_config: expected 'prd', got '{instance.get('doppler_config')}'"

    # Required secrets (default: no OSS routing or mesh telemetry)
    required_secrets = instance.get("required_secrets", {})
    github_secrets = required_secrets.get("github", [])
    assert github_secrets == ["ANTHROPIC_API_KEY"], (
        f"required_secrets.github: expected ['ANTHROPIC_API_KEY'], got {github_secrets}"
    )

    doppler_secrets = required_secrets.get("doppler", {})
    doppler_keys = doppler_secrets.get("keys", [])
    assert doppler_keys == [], f"required_secrets.doppler.keys: expected [], got {doppler_keys}"

    fly_secrets = required_secrets.get("fly", [])
    assert fly_secrets == [], f"required_secrets.fly: expected [], got {fly_secrets}"

    # Repo vars (default: none)
    repo_vars = instance.get("repo_vars", [])
    assert repo_vars == [], f"instance.repo_vars: expected [], got {repo_vars}"

    # Components
    components = data.get("components", {})
    litellm_proxy = components.get("litellm_proxy", {})
    assert litellm_proxy.get("enabled") is False, (
        f"components.litellm_proxy.enabled: expected False, got {litellm_proxy.get('enabled')}"
    )


def test_pack_components_flags(tmp_path):
    """Verify pack-components.yml with oss_routing and mesh_telemetry flags."""
    dst = tmp_path / "flags_render"
    dst.mkdir()
    render(dst, oss_routing="true", mesh_telemetry="true")

    pack_file = dst / "pack-components.yml"
    assert pack_file.exists(), "pack-components.yml missing"

    data = yaml.safe_load(pack_file.read_text())
    instance = data.get("instance", {})
    required_secrets = instance.get("required_secrets", {})

    # Repo vars should include both flags
    repo_vars = instance.get("repo_vars", [])
    assert set(repo_vars) == {"LITELLM_PROXY_URL", "MESH_INVOCATION_URL"}, (
        f"instance.repo_vars: expected {{'LITELLM_PROXY_URL', 'MESH_INVOCATION_URL'}}, got {set(repo_vars)}"
    )

    # GitHub secrets
    github_secrets = required_secrets.get("github", [])
    expected_github = {"DOPPLER_SERVICE_TOKEN_LITELLM", "FLY_API_TOKEN", "LITELLM_MASTER_KEY"}  # ANTHROPIC_API_KEY not required on the proxy plane
    assert set(github_secrets) == expected_github, (
        f"required_secrets.github: expected {expected_github}, got {set(github_secrets)}"
    )

    # Doppler keys
    doppler_secrets = required_secrets.get("doppler", {})
    doppler_keys = doppler_secrets.get("keys", [])
    expected_doppler = {"LITELLM_MASTER_KEY", "MESH_BEARER_AGENT"}
    assert set(doppler_keys) == expected_doppler, (
        f"required_secrets.doppler.keys: expected {expected_doppler}, got {set(doppler_keys)}"
    )

    # Fly secrets
    fly_secrets = required_secrets.get("fly", [])
    assert fly_secrets == ["LITELLM_MASTER_KEY"], (
        f"required_secrets.fly: expected ['LITELLM_MASTER_KEY'], got {fly_secrets}"
    )

    # Components
    components = data.get("components", {})
    litellm_proxy = components.get("litellm_proxy", {})
    assert litellm_proxy.get("enabled") is True, (
        f"components.litellm_proxy.enabled: expected True, got {litellm_proxy.get('enabled')}"
    )


def test_otel_endpoint_answer(tmp_path):
    """Verify .claude/settings.json OTEL_EXPORTER_OTLP_ENDPOINT contract."""
    # Telemetry render (langfuse_telemetry on) should have the Langfuse endpoint
    telemetry_dst = tmp_path / "telemetry"
    telemetry_dst.mkdir()
    render(telemetry_dst, langfuse_telemetry="true")
    default_settings = telemetry_dst / ".claude" / "settings.json"
    assert default_settings.exists(), ".claude/settings.json missing in telemetry render"

    default_content = json.loads(default_settings.read_text())
    default_otel = default_content.get("env", {}).get("OTEL_EXPORTER_OTLP_ENDPOINT")
    assert default_otel == "https://us.cloud.langfuse.com/api/public/otel", (
        f"default OTEL_EXPORTER_OTLP_ENDPOINT: expected Langfuse URL, got '{default_otel}'"
    )

    # Fresh render with custom otel_endpoint
    custom_dst = tmp_path / "custom_otel"
    custom_dst.mkdir()
    render(custom_dst, otel_endpoint="https://otel.example.com/v1", langfuse_telemetry="true")

    custom_settings = custom_dst / ".claude" / "settings.json"
    assert custom_settings.exists(), ".claude/settings.json missing in custom render"

    custom_content = json.loads(custom_settings.read_text())
    custom_otel = custom_content.get("env", {}).get("OTEL_EXPORTER_OTLP_ENDPOINT")
    assert custom_otel == "https://otel.example.com/v1", (
        f"custom OTEL_EXPORTER_OTLP_ENDPOINT: expected custom URL, got '{custom_otel}'"
    )
    assert "langfuse" not in custom_otel.lower(), (
        f"custom OTEL_EXPORTER_OTLP_ENDPOINT contains 'langfuse': {custom_otel}"
    )


def test_verify_secrets_cli(tmp_path):
    """Verify scripts/first-run-setup.py --verify-secrets contract."""
    # Render with oss_routing=true (requires LITELLM_MASTER_KEY)
    oss_dst = tmp_path / "oss_render"
    oss_dst.mkdir()
    render(oss_dst, oss_routing="true")

    # Run without LITELLM_MASTER_KEY in env — should fail with returncode 2
    env_without_key = {k: v for k, v in os.environ.items() if k != "LITELLM_MASTER_KEY"}
    result = subprocess.run(
        [sys.executable, "scripts/first-run-setup.py", "--verify-secrets"],
        check=False,
        cwd=oss_dst,
        capture_output=True,
        text=True,
        env=env_without_key,
    )
    assert result.returncode == 2, (
        f"Expected returncode 2 (missing key), got {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "LITELLM_MASTER_KEY" in result.stdout, (
        f"Expected 'LITELLM_MASTER_KEY' in stdout, got: {result.stdout}"
    )

    # Run with LITELLM_MASTER_KEY=test-value — should succeed with returncode 0
    env_with_key = {**env_without_key, "LITELLM_MASTER_KEY": "test-value"}
    result = subprocess.run(
        [sys.executable, "scripts/first-run-setup.py", "--verify-secrets"],
        check=False,
        cwd=oss_dst,
        capture_output=True,
        text=True,
        env=env_with_key,
    )
    assert result.returncode == 0, (
        f"Expected returncode 0 (all keys present), got {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    # Default render (no oss_routing) should require no doppler keys — should succeed with returncode 0
    default_dst = tmp_path / "default_verify"
    default_dst.mkdir()
    render(default_dst)

    result = subprocess.run(
        [sys.executable, "scripts/first-run-setup.py", "--verify-secrets"],
        check=False,
        cwd=default_dst,
        capture_output=True,
        text=True,
        env=env_without_key,
    )
    assert result.returncode == 0, (
        f"Expected returncode 0 (default render has no doppler keys), got {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_claude_hooks_render_includes_handler_and_event(default_render):
    settings = json.loads((default_render / ".claude" / "settings.json").read_text())
    assert "InstructionsLoaded" in settings["hooks"]
    handler = default_render / "scripts" / "instruction-load-audit.py"
    verifier = default_render / "scripts" / "verify-instruction-load-audit.py"
    assert handler.is_file() and os.access(handler, os.X_OK)
    assert verifier.is_file()
    ignore = (default_render / ".gitignore").read_text()
    assert "instruction-loads.jsonl" in ignore
    assert ".ai/memory/*.lock" in ignore
    canary = json.loads((default_render / "config" / "instruction-load-canary.json").read_text())
    assert canary["requirements"][0]["path"] == "CLAUDE.md"
    command = settings["hooks"]["InstructionsLoaded"][0]["hooks"][0]["command"]
    assert "instruction-load-audit.py" in command
    assert handler.exists()


def test_claude_hooks_false_does_not_claim_host_event(tmp_path):
    dst = tmp_path / "no-claude"
    dst.mkdir()
    render(dst, claude_hooks="false")
    settings = json.loads((dst / ".claude" / "settings.json").read_text())
    assert "InstructionsLoaded" not in settings.get("hooks", {})
    docs = (dst / "docs" / "instruction-load-observability.md").read_text()
    assert "Codex and Cursor do not emit this event" in docs
    agents = (dst / "AGENTS.md").read_text()
    assert "do not invent one" in agents


TELEMETRY_ENV = {
    "CLAUDE_CODE_ENABLE_TELEMETRY", "OTEL_METRICS_EXPORTER", "OTEL_TRACES_EXPORTER",
    "OTEL_LOGS_EXPORTER", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_PROTOCOL",
}
STRICT_EXCLUDED = {
    ".github/workflows/codex-pr-review.yml",
    ".github/workflows/codex-security-diff-scan.yml",
    ".github/workflows/setup-env-secret.yml",
    ".github/workflows/dependabot-manual-automerge.yml",
    ".github/workflows/monitor-litellm.yml",
    ".github/workflows/publish-litellm-product-release.yml",
    "deploy/litellm-proxy/config.yaml",
    "scripts/setup-litellm.sh",
    "config/litellm-effective-config.example.yaml",
    "config/litellm-product-profile.example.json",
    "scripts/build-litellm-product-asset.py",
    "scripts/render-litellm-product-profile.py",
    "tests/test_litellm_product_asset.py",
    "tests/test_render_litellm_product_profile.py",
    "scripts/tests/test_dependabot_manual_automerge.py",
    "tests/test_openrouter_retention_policy.py",
}


@pytest.fixture(scope="session")
def strict_render(tmp_path_factory):
    dst = tmp_path_factory.mktemp("strict")
    render(dst, isolation_profile="strict")
    return dst


def test_telemetry_keys_follow_the_flag(default_render, tmp_path):
    """Telemetry env + otelHeadersHelper only when langfuse_telemetry is on."""
    default = json.loads((default_render / ".claude/settings.json").read_text())
    assert not TELEMETRY_ENV & set(default["env"])
    assert "otelHeadersHelper" not in default
    # Everything else in the standard profile is unchanged.
    assert default["skipDangerousModePermissionPrompt"] is True
    assert default["permissions"]["defaultMode"] == "bypassPermissions"
    assert default["permissions"]["allow"]
    dst = tmp_path / "telemetry"
    dst.mkdir()
    render(dst, langfuse_telemetry="true")
    on = json.loads((dst / ".claude/settings.json").read_text())
    assert TELEMETRY_ENV <= set(on["env"])
    assert on["otelHeadersHelper"] == "scripts/otel-langfuse-headers.sh"
    assert {k: v for k, v in on.items() if k not in ("env", "otelHeadersHelper")} == \
        {k: v for k, v in default.items() if k not in ("env", "otelHeadersHelper")}


@pytest.mark.parametrize("render_fixture", ["default_render", "strict_render"])
def test_rendered_test_suite_collects(render_fixture, request):
    """A pack-only test that ships into a render aborts the instance's whole
    pytest run at collection (tests/test_devin_meta_plugin.py did, v1.23.0
    cycle). Collection must succeed in every profile."""
    dst = request.getfixturevalue(render_fixture)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=dst, capture_output=True, text=True, timeout=600,
        # The renders are shared session fixtures: leave no __pycache__ behind
        # for the file-set comparisons in other tests.
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    errors = [line for line in proc.stdout.splitlines() if line.startswith("ERROR ")]
    assert not errors, "\n".join(errors)
    # A conftest import error reports on stderr with a nonzero exit and no
    # `ERROR ` line on stdout (CodeRabbit review, PR #247).
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "tests/test_devin_meta_plugin.py" not in proc.stdout


def test_strict_settings_are_hooks_and_deny_only(strict_render, default_render):
    strict = json.loads((strict_render / ".claude/settings.json").read_text())
    default = json.loads((default_render / ".claude/settings.json").read_text())
    assert set(strict) == {"permissions", "hooks"}
    assert strict["permissions"] == {"deny": default["permissions"]["deny"]}
    assert strict["hooks"] == default["hooks"]


def test_strict_excludes_manolii_coupled_files(strict_render, default_render):
    default_files = file_set(default_render)
    strict_files = file_set(strict_render)
    assert STRICT_EXCLUDED <= default_files
    assert not STRICT_EXCLUDED & strict_files
    assert not any(f.startswith(("deploy/litellm-proxy/", "litellm-product/")) for f in strict_files)
    removed = default_files - strict_files
    assert removed <= STRICT_EXCLUDED | {f for f in default_files
                                         if f.startswith(("deploy/litellm-proxy/", "litellm-product/"))}
    answers = yaml.safe_load((strict_render / ".copier-answers.yml").read_text())
    assert answers["isolation_profile"] == "strict" and answers["codex_review"] is False
    components = yaml.safe_load((strict_render / "pack-components.yml").read_text())
    assert components["instance"]["isolation_profile"] == "strict"


def test_strict_readme_does_not_point_at_unrendered_proxy_assets(strict_render, default_render):
    """Codex review, PR #238: strict renders must not instruct users to run or
    read the LiteLLM files the strict profile excludes."""
    strict = (strict_render / "README-STARTER-PACK.md").read_text()
    for ref in ("setup-litellm.sh", "litellm-product/README.md", "deploy/litellm-proxy",
                "config/litellm-product-profile.example.json"):
        assert ref not in strict, ref
    assert "isolation_profile=strict" in strict
    default = (default_render / "README-STARTER-PACK.md").read_text()
    assert "bash scripts/setup-litellm.sh" in default and "deploy/litellm-proxy/" in default


def test_standard_keeps_codex_review_unless_opted_out(default_render, tmp_path):
    codex = {".github/workflows/codex-pr-review.yml", ".github/workflows/codex-security-diff-scan.yml"}
    assert codex <= file_set(default_render)
    dst = tmp_path / "nocodex"
    dst.mkdir()
    render(dst, codex_review="false")
    assert file_set(default_render) - file_set(dst) == codex


@pytest.mark.parametrize("flag", ["oss_routing", "kl_integration", "langfuse_telemetry", "mesh_telemetry",
                                  "codex_review"])
def test_strict_refuses_manolii_hosted_services(tmp_path, flag):
    with pytest.raises(subprocess.CalledProcessError):
        render(tmp_path / "bad", isolation_profile="strict", **{flag: "true"})
