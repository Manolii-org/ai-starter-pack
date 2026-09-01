#!/usr/bin/env python3
"""Execute the shipped deployment receipt and held-SHA action programs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
EMITTER = ROOT / ".github/actions/emit-deployment-receipt/emit.py"
SCHEMA = ROOT / ".github/actions/emit-deployment-receipt/deployment-receipt.schema.json"
RESOLVER = ROOT / ".github/actions/resolve-held-promote-sha/resolve.py"
SHA = "a" * 40


def emitter_env(tmp_path: Path, result: str = "success") -> dict[str, str]:
    return {
        **os.environ,
        "INPUT_EMIT_WHEN": "alias-confirmed",
        "INPUT_TARGET_SHA": SHA,
        "INPUT_CANONICAL_URL": "https://held.example.com",
        "INPUT_LIVE_ORIGIN": "",
        "INPUT_PREVIOUS_DEPLOYMENT_ID": "dpl_previous",
        "INPUT_MIGRATION_PENDING": "",
        "INPUT_VERIFY_WORKFLOW": ".github/workflows/smoke-prod.yml",
        "INPUT_VERIFY_RESULT": result,
        "INPUT_VERIFY_RUN_URL": "https://github.com/o/r/actions/runs/2",
        "INPUT_GATE_RUN_URL": "https://github.com/o/r/actions/runs/1",
        "INPUT_PROMOTE_RUN_URL": "https://github.com/o/r/actions/runs/3",
        "INPUT_REPO": "o/r",
        "GITHUB_REPOSITORY": "o/r",
        "INPUT_ROLLBACK_MODE": "alias-promote",
        "RECEIPT_OUTPUT": str(tmp_path / "deployment-receipt.json"),
    }


@pytest.mark.parametrize("result", ["success", "failure", "skipped"])
def test_emitter_executes_and_schema_validates(tmp_path, result):
    process = subprocess.run(
        [sys.executable, str(EMITTER)],
        env=emitter_env(tmp_path, result),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    receipt = json.loads((tmp_path / "deployment-receipt.json").read_text())
    jsonschema.validate(
        receipt,
        json.loads(SCHEMA.read_text()),
        format_checker=jsonschema.FormatChecker(),
    )
    assert receipt["target_sha"] == SHA
    assert receipt["post_promote_verify"]["result"] == result
    assert "live_origin" not in receipt


@pytest.mark.parametrize(("value", "expected"), [("0", 0), ("1", 1), ("12", 12)])
def test_emitter_preserves_optional_migration_pending(tmp_path, value, expected):
    env = emitter_env(tmp_path)
    env["INPUT_MIGRATION_PENDING"] = value
    process = subprocess.run(
        [sys.executable, str(EMITTER)], env=env, capture_output=True, text=True, timeout=30
    )
    assert process.returncode == 0, process.stderr
    receipt = json.loads((tmp_path / "deployment-receipt.json").read_text())
    assert receipt["migration_pending"] == expected


@pytest.mark.parametrize("value", ["-1", "true", "1.5"])
def test_emitter_rejects_invalid_migration_pending(tmp_path, value):
    env = emitter_env(tmp_path)
    env["INPUT_MIGRATION_PENDING"] = value
    process = subprocess.run(
        [sys.executable, str(EMITTER)], env=env, capture_output=True, text=True, timeout=30
    )
    assert process.returncode == 1
    assert "migration_pending must be a non-negative integer" in process.stderr
    assert not (tmp_path / "deployment-receipt.json").exists()


@pytest.mark.parametrize(
    ("name", "value"),
    [("INPUT_TARGET_SHA", ""), ("INPUT_TARGET_SHA", "abc"),
     ("INPUT_VERIFY_RESULT", "in_progress"), ("INPUT_EMIT_WHEN", "smoke-finished")],
)
def test_emitter_fails_closed_on_invalid_inputs(tmp_path, name, value):
    env = emitter_env(tmp_path)
    env[name] = value
    process = subprocess.run(
        [sys.executable, str(EMITTER)], env=env, capture_output=True, text=True, timeout=30
    )
    assert process.returncode == 1
    assert not (tmp_path / "deployment-receipt.json").exists()


def test_emitter_rejects_cross_repo_receipt(tmp_path):
    env = emitter_env(tmp_path)
    env["INPUT_REPO"] = "other/repo"
    process = subprocess.run(
        [sys.executable, str(EMITTER)], env=env, capture_output=True, text=True, timeout=30
    )
    assert process.returncode == 1
    assert "repo must match GITHUB_REPOSITORY" in process.stderr


def test_emitter_rejects_non_uri_canonical_url(tmp_path):
    env = emitter_env(tmp_path)
    env["INPUT_CANONICAL_URL"] = "not-a-url"
    process = subprocess.run(
        [sys.executable, str(EMITTER)], env=env, capture_output=True, text=True, timeout=30
    )
    assert process.returncode == 1
    assert "canonical_url must be an absolute URI" in process.stderr


def _fake_gh(tmp_path: Path, status: str = "behind") -> Path:
    executable = tmp_path / "gh"
    executable.write_text(
        """#!/usr/bin/env python3
import json, sys
path = sys.argv[-1]
if path == 'repos/o/r':
    print(json.dumps({'default_branch': 'main'}))
elif path == 'repos/o/r/compare/main...""" + SHA + """':
    print(json.dumps({'status': '""" + status + """'}))
else:
    print('unexpected path: ' + path, file=sys.stderr)
    raise SystemExit(2)
"""
    )
    executable.chmod(0o755)
    return executable


def test_resolver_executes_against_default_branch(tmp_path):
    _fake_gh(tmp_path)
    output = tmp_path / "output"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "INPUT_SHA": SHA,
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_OUTPUT": str(output),
    }
    process = subprocess.run(
        [sys.executable, str(RESOLVER)], env=env, capture_output=True, text=True, timeout=30
    )
    assert process.returncode == 0, process.stderr
    assert output.read_text() == f"sha={SHA}\ndefault_branch=main\n"


def test_resolver_rejects_empty_sha_without_calling_gh(tmp_path):
    env = {
        **os.environ,
        "PATH": str(tmp_path),
        "INPUT_SHA": "",
        "GITHUB_REPOSITORY": "o/r",
    }
    process = subprocess.run(
        [sys.executable, str(RESOLVER)], env=env, capture_output=True, text=True, timeout=30
    )
    assert process.returncode == 1
    assert "sha is required" in process.stderr


def test_resolver_rejects_sha_ahead_of_default_branch(tmp_path):
    _fake_gh(tmp_path, "ahead")
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "INPUT_SHA": SHA,
        "GITHUB_REPOSITORY": "o/r",
    }
    process = subprocess.run(
        [sys.executable, str(RESOLVER)], env=env, capture_output=True, text=True, timeout=30
    )
    assert process.returncode == 1
    assert "expected identical or behind" in process.stderr


def test_composite_uploads_before_release_pointer():
    action = yaml.safe_load(
        (ROOT / ".github/actions/emit-deployment-receipt/action.yml").read_text()
    )
    names = [step["name"] for step in action["runs"]["steps"]]
    assert names.index("Upload deployment receipt artifact") < names.index(
        "Write durable production receipt pointer"
    )
    upload = next(
        step for step in action["runs"]["steps"]
        if step["name"] == "Upload deployment receipt artifact"
    )
    assert upload["with"]["retention-days"] == 30


def test_release_pointer_uploads_safely_and_bounds_api_calls():
    action = yaml.safe_load(
        (ROOT / ".github/actions/emit-deployment-receipt/action.yml").read_text()
    )
    release_step = next(
        step for step in action["runs"]["steps"]
        if step["name"] == "Write durable production receipt pointer"
    )
    script = release_step["run"]
    assert "timeout 30s gh api" in script
    assert "timeout 30s gh release" in script
    assert "--clobber" not in script
    assert script.index("release upload production-receipt") < script.index(
        "git/refs/tags/production-receipt"
    )
    assert '-f name="deployment-receipt.json"' in script
    assert "trap rollback_pointer ERR" in script
    assert 'exit "$rc"' in script


def test_composite_bounds_schema_dependency_install():
    action = yaml.safe_load(
        (ROOT / ".github/actions/emit-deployment-receipt/action.yml").read_text()
    )
    install = next(
        step for step in action["runs"]["steps"]
        if step["name"] == "Install receipt schema validator"
    )
    assert install["run"].startswith("timeout 60s python3 -m pip install")


def test_docs_require_single_writer_concurrency():
    docs = (ROOT / ".github/workflows/REUSABLE-WORKFLOWS.md").read_text()
    assert "Callers must serialize every invocation for a product" in docs
    assert "cancel-in-progress: false" in docs


def test_both_examples_run_receipt_after_a_failed_smoke():
    examples = yaml.safe_load((ROOT / "docs/examples/held-promote-receipts.yml").read_text())
    assert len(examples) == 2
    for step in examples:
        condition = step["if"]
        assert "always()" in condition
        assert "!cancelled()" in condition
