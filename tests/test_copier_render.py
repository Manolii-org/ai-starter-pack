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
