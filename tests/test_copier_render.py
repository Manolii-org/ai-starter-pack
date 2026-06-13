#!/usr/bin/env python3
"""Test suite for Copier template rendering (copier.yml)."""
import hashlib
import os
import re
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

# Gated features: flag name → set of files that only exist when flag is true
GATED = {
    "oss_routing": {"docs/us-oss-eligibility-matrix.md"},
    "browserbase": {
        ".claude/commands/browse.md",
        ".claude/commands/nav-record.md",
        ".claude/commands/nav-replay.md",
    },
    "codex_adversarial": {".claude/agents/codex-adversarial.md"},
    "kl_integration": set(),
    "langfuse_telemetry": set(),
    "mesh_telemetry": {"mesh-contract.yaml.template"},
}


def render(dst, **data):
    """Render template to dst directory. Bool values as 'true'/'false' strings."""
    cmd = [
        sys.executable,
        "-m",
        "copier",
        "copy",
        "--defaults",
        "--quiet",
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


def test_no_render_corruption(default_render):
    """Post-render corruption lint: verify no blind-sed artifacts remain.

    Covers §8-N2 defect class: doubled backslash escapes, tier-mixing, OSS-only markers.
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
        assert not DOUBLED_BACKSLASH_ESCAPE_RE.search(text), (
            f"{p} contains doubled-backslash escape (\\\\n, \\\\r, or \\\\t)"
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

    # Check gated files absent (all flags off by default)
    for flag_files in GATED.values():
        for gated_file in flag_files:
            assert not (default_render / gated_file).exists(), f"Gated file present: {gated_file}"

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


def test_flag_matrix():
    """For each flag in GATED: assert flag_render files = default_render files + GATED[flag]."""
    with tempfile.TemporaryDirectory() as tmpdir:
        default_dst = Path(tmpdir) / "default"
        default_dst.mkdir()
        render(default_dst)
        default_files = file_set(default_dst)

        for flag, flag_files in GATED.items():
            with tempfile.TemporaryDirectory() as flag_tmpdir:
                flag_dst = Path(flag_tmpdir) / flag
                flag_dst.mkdir()
                render(flag_dst, **{flag: "true"})
                flag_render_files = file_set(flag_dst)

                # flag_render should == default + GATED[flag]
                expected = default_files | flag_files
                assert flag_render_files == expected, (
                    f"{flag}: unexpected diff.\n"
                    f"Extra: {flag_render_files - expected}\n"
                    f"Missing: {expected - flag_render_files}"
                )


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


def test_skip_if_exists_contract():
    """Verify _skip_if_exists in copier.yml and behavioural guarantee."""
    copier_config = yaml.safe_load((ROOT / "copier.yml").read_text())
    skip_if_exists = set(copier_config.get("_skip_if_exists", []))

    expected = {"CLAUDE.md", ".claude/model-routing.json", ".claude/mcp.json", ".claude/hooks/session-start.sh"}
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

        # Re-render with --overwrite
        cmd = [
            sys.executable,
            "-m",
            "copier",
            "copy",
            "--defaults",
            "--quiet",
            "--overwrite",
            str(ROOT),
            str(dst),
        ]
        subprocess.run(cmd, check=True)

        # Sentinel should survive
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
    expected_github = {"ANTHROPIC_API_KEY", "DOPPLER_SERVICE_TOKEN_LITELLM", "FLY_API_TOKEN", "LITELLM_MASTER_KEY"}
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


def test_otel_endpoint_answer(default_render, tmp_path):
    """Verify .claude/settings.json OTEL_EXPORTER_OTLP_ENDPOINT contract."""
    # Default render should have the Langfuse endpoint
    default_settings = default_render / ".claude" / "settings.json"
    assert default_settings.exists(), ".claude/settings.json missing in default render"

    import json
    default_content = json.loads(default_settings.read_text())
    default_otel = default_content.get("env", {}).get("OTEL_EXPORTER_OTLP_ENDPOINT")
    assert default_otel == "https://us.cloud.langfuse.com/api/public/otel", (
        f"default OTEL_EXPORTER_OTLP_ENDPOINT: expected Langfuse URL, got '{default_otel}'"
    )

    # Fresh render with custom otel_endpoint
    custom_dst = tmp_path / "custom_otel"
    custom_dst.mkdir()
    render(custom_dst, otel_endpoint="https://otel.example.com/v1")

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
        cwd=default_dst,
        capture_output=True,
        text=True,
        env=env_without_key,
    )
    assert result.returncode == 0, (
        f"Expected returncode 0 (default render has no doppler keys), got {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
