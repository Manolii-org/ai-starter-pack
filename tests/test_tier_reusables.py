"""Fail-closed coverage for the CI tier reusable workflows.

The three tier reusables (`fast-tier-reusable.yml`,
`pre-production-tier-reusable.yml`, `tier-gate-summary-reusable.yml`) are
published for OTHER repositories to call by version tag. Their whole purpose
is to make a skipped gate impossible to mistake for a passed one, which means
every fail-OPEN path is a defect that reports green — the failure mode least
likely to be noticed by the consumer it hurts.

`tier-reusables-selftest.yml` executes the POSITIVE path on GitHub for real.
It cannot cover the negative path: a job that calls a reusable workflow may
not use `continue-on-error` (GitHub restricts callers to name/uses/with/
secrets/needs/if/permissions), so a deliberately-failing tier would turn the
whole self-test run red with no way to invert it.

So the negatives are covered here instead, by extracting the SHIPPED script
out of the workflow YAML and running it directly. The workflow stays the
single source of truth — these tests read it rather than restating it, so a
change to the logic that breaks the contract fails here even though nothing
was duplicated.

Run: python3 -m pytest tests/test_tier_reusables.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml  # hard import, never importorskip: a missing dep must not silently
# skip every fail-closed assertion in this file and leave CI green.

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

FAST = WORKFLOWS / "fast-tier-reusable.yml"
PREPROD = WORKFLOWS / "pre-production-tier-reusable.yml"
SUMMARY = WORKFLOWS / "tier-gate-summary-reusable.yml"
SELFTEST = WORKFLOWS / "tier-reusables-selftest.yml"

TIER_WORKFLOWS = (FAST, PREPROD)


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def step_run(workflow: Path, job: str, step_name: str) -> str:
    """Return the `run:` body of a named step, or fail loudly.

    Locating steps by NAME rather than index means reordering the steps does
    not silently pick up the wrong script and start asserting nothing.
    """
    data = load(workflow)
    steps = data["jobs"][job]["steps"]
    for step in steps:
        if step.get("name") == step_name:
            assert "run" in step, f"{workflow.name}:{job}:{step_name} has no `run:`"
            return step["run"]
    available = [s.get("name") for s in steps]
    raise AssertionError(
        f"{workflow.name}: no step named {step_name!r} in job {job!r}. "
        f"Present: {available}"
    )


def extract_heredoc(script: str, tag: str = "PY") -> str:
    """Pull the body out of a `python3 <<'PY' ... PY` heredoc."""
    open_marker = f"<<'{tag}'"
    assert open_marker in script, f"no {open_marker} heredoc found in step"
    body = script.split(open_marker, 1)[1].lstrip("\n")
    lines = []
    for line in body.split("\n"):
        if line.strip() == tag:
            break
        lines.append(line)
    else:  # pragma: no cover - defensive
        raise AssertionError(f"unterminated {tag} heredoc")
    return textwrap.dedent("\n".join(lines))


def run_validator(workflow: Path, gates: str) -> subprocess.CompletedProcess:
    """Execute the shipped `Validate gates JSON` script against a spec."""
    source = extract_heredoc(step_run(workflow, "validate", "Validate gates JSON"))
    return subprocess.run(
        [sys.executable, "-c", source],
        env={**os.environ, "GATES_JSON": gates},
        capture_output=True,
        text=True,
        timeout=60,
    )


# ── The gate spec validator: every rejection is a closed fail-open path ────

REJECTED = {
    "empty array": "[]",
    "not JSON": "not json at all",
    "not an array": '{"name":"x"}',
    "element not an object": '["lint"]',
    "missing name": '[{"applies":true,"command":"true"}]',
    "blank name": '[{"name":"   ","applies":true,"command":"true"}]',
    "newline in name": '[{"name":"a\\nb","applies":true,"command":"true"}]',
    "missing applies": '[{"name":"lint","command":"true"}]',
    "applies as string": '[{"name":"lint","applies":"true","command":"true"}]',
    "applies as int": '[{"name":"lint","applies":1,"command":"true"}]',
    "applies true without command": '[{"name":"lint","applies":true}]',
    "applies true with blank command": '[{"name":"lint","applies":true,"command":"  "}]',
    "slug collision": (
        '[{"name":"a b","applies":false},{"name":"a_b","applies":false}]'
    ),
}


@pytest.mark.parametrize("workflow", TIER_WORKFLOWS, ids=lambda p: p.stem)
@pytest.mark.parametrize("case", sorted(REJECTED), ids=lambda s: s.replace(" ", "-"))
def test_validator_rejects_fail_open_specs(workflow: Path, case: str) -> None:
    """Each of these would let the tier report green having verified nothing."""
    result = run_validator(workflow, REJECTED[case])
    assert result.returncode != 0, (
        f"{workflow.name} ACCEPTED a {case!r} gate spec. "
        f"The tier would report green without assurance.\n"
        f"stdout: {result.stdout}"
    )
    assert "::error::" in result.stdout, (
        f"{workflow.name} rejected {case!r} without an ::error:: annotation, "
        "so the reason will not surface in the Actions UI."
    )


@pytest.mark.parametrize("workflow", TIER_WORKFLOWS, ids=lambda p: p.stem)
def test_validator_accepts_a_mixed_spec(workflow: Path) -> None:
    """A real spec — one applied gate, one skipped — must pass."""
    gates = json.dumps(
        [
            {"name": "lint", "applies": True, "command": "ruff check ."},
            {"name": "migrations", "applies": False, "skip_reason": "none changed"},
        ]
    )
    result = run_validator(workflow, gates)
    assert result.returncode == 0, (
        f"{workflow.name} rejected a valid spec:\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.parametrize("workflow", TIER_WORKFLOWS, ids=lambda p: p.stem)
def test_slug_collision_check_matches_runtime_slug(workflow: Path) -> None:
    """Validation-time and runtime slugs must agree on non-ASCII names.

    The runtime slug is `tr -c '[:alnum:]._-' '_'`, which works on UTF-8
    BYTES in the C locale — "é" becomes TWO underscores. If the validator
    used Python's `str.isalnum()` it would keep "é" intact, so "é" and "__"
    would validate as distinct, then collide on disk and lose a verdict.
    """
    gates = json.dumps(
        [
            {"name": "é", "applies": False},
            {"name": "__", "applies": False},
        ]
    )
    result = run_validator(workflow, gates)
    assert result.returncode != 0, (
        f"{workflow.name} accepted two names that collide under the runtime "
        "byte-wise slug — one gate's verdict would silently overwrite the other."
    )


# ── The aggregate: a missing verdict must never count as a pass ────────────


def run_aggregate(
    tmp_path: Path,
    workflow: Path,
    *,
    gates: str,
    verdicts: dict[str, str],
    validate_result: str = "success",
    needs_result: str = "success",
) -> tuple[int, dict[str, str], str]:
    """Execute the shipped `Summarise tier` bash against fabricated verdicts."""
    script = step_run(workflow, "aggregate", "Summarise tier")
    verdict_dir = tmp_path / "verdicts"
    verdict_dir.mkdir()
    for name, verdict in verdicts.items():
        (verdict_dir / f"gate-{name}.txt").write_text(f"{verdict} {name}\n")

    github_output = tmp_path / "output"
    github_output.touch()
    summary = tmp_path / "summary"
    summary.touch()

    proc = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "GATES_JSON": gates,
            "NEEDS_RESULT": needs_result,
            "VALIDATE_RESULT": validate_result,
            "BUDGET": "5",
            "GITHUB_OUTPUT": str(github_output),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    parsed: dict[str, str] = {}
    for line in github_output.read_text().splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            parsed[key] = value
    return proc.returncode, parsed, proc.stdout + proc.stderr


TWO_GATES = json.dumps(
    [
        {"name": "a", "applies": True, "command": "true"},
        {"name": "b", "applies": False, "skip_reason": "out of scope"},
    ]
)


@pytest.mark.parametrize("workflow", TIER_WORKFLOWS, ids=lambda p: p.stem)
def test_aggregate_passes_with_a_visible_skip(tmp_path: Path, workflow: Path) -> None:
    """pass + skip is a passing tier, and the skip stays counted separately."""
    rc, out, log = run_aggregate(
        tmp_path, workflow, gates=TWO_GATES, verdicts={"a": "pass", "b": "skip"}
    )
    assert rc == 0, f"{workflow.name} failed a legitimate pass+skip tier:\n{log}"
    assert out.get("verdict") == "pass"
    assert json.loads(out["breakdown"]) == {"pass": 1, "skip": 1, "fail": 0}, (
        "a skip must be reported as a skip, never folded into the pass count"
    )


@pytest.mark.parametrize("workflow", TIER_WORKFLOWS, ids=lambda p: p.stem)
def test_aggregate_fails_on_a_missing_verdict(tmp_path: Path, workflow: Path) -> None:
    """A gate that died before reporting must NOT be counted as passing.

    This is the headline fail-open: a cancelled or crashed gate job leaves no
    artifact, and a naive aggregate that only counts what it finds reports a
    green tier for a gate that never ran.
    """
    rc, out, log = run_aggregate(
        tmp_path, workflow, gates=TWO_GATES, verdicts={"a": "pass"}
    )
    assert rc != 0, (
        f"{workflow.name} PASSED with a missing gate verdict — a gate that "
        f"never reported was silently treated as fine.\n{log}"
    )
    assert out.get("verdict") == "fail"


@pytest.mark.parametrize("workflow", TIER_WORKFLOWS, ids=lambda p: p.stem)
def test_aggregate_fails_when_validation_did_not_succeed(
    tmp_path: Path, workflow: Path
) -> None:
    """`if: always()` runs the aggregate even when the spec never validated."""
    rc, out, _ = run_aggregate(
        tmp_path,
        workflow,
        gates=TWO_GATES,
        verdicts={},
        validate_result="failure",
    )
    assert rc != 0 and out.get("verdict") == "fail"


@pytest.mark.parametrize("workflow", TIER_WORKFLOWS, ids=lambda p: p.stem)
def test_aggregate_fails_on_a_failed_verdict(tmp_path: Path, workflow: Path) -> None:
    rc, out, _ = run_aggregate(
        tmp_path, workflow, gates=TWO_GATES, verdicts={"a": "fail", "b": "skip"}
    )
    assert rc != 0 and out.get("verdict") == "fail"


@pytest.mark.parametrize("workflow", TIER_WORKFLOWS, ids=lambda p: p.stem)
@pytest.mark.parametrize("gate_result", ["failure", "cancelled"])
def test_aggregate_fails_when_the_gate_job_did_not_succeed(
    tmp_path: Path, workflow: Path, gate_result: str
) -> None:
    """Verdict artifacts can look complete while the job itself died."""
    rc, out, _ = run_aggregate(
        tmp_path,
        workflow,
        gates=TWO_GATES,
        verdicts={"a": "pass", "b": "skip"},
        needs_result=gate_result,
    )
    assert rc != 0, (
        f"{workflow.name} passed while its gate job resolved {gate_result!r}"
    )
    assert out.get("verdict") == "fail"


# ── Structural contract: what consumers pin a tag against ─────────────────


@pytest.mark.parametrize("workflow", TIER_WORKFLOWS + (SUMMARY,), ids=lambda p: p.stem)
def test_workflow_is_callable_and_least_privileged(workflow: Path) -> None:
    data = load(workflow)
    # PyYAML parses a bare `on:` key as the boolean True (the Norway problem).
    triggers = data.get("on", data.get(True))
    assert isinstance(triggers, dict) and "workflow_call" in triggers, (
        f"{workflow.name} must be callable — consumers pin it with `uses:`"
    )
    assert data.get("permissions") == {"contents": "read"}, (
        f"{workflow.name} must declare least-privilege top-level permissions; "
        f"got {data.get('permissions')!r}"
    )


@pytest.mark.parametrize("workflow", TIER_WORKFLOWS, ids=lambda p: p.stem)
def test_aggregate_job_name_is_the_branch_protection_anchor(workflow: Path) -> None:
    """The aggregate is the ONE check a consumer names in branch protection.

    Renaming it silently orphans every consumer's required check, which then
    can never report and blocks their merges forever. That is a breaking
    change requiring a major-version tag, so pin the names here.
    """
    expected = {"fast-tier-reusable": "fast-tier", "pre-production-tier-reusable": "pre-production-tier"}
    data = load(workflow)
    assert data["jobs"]["aggregate"]["name"] == expected[workflow.stem]
    assert data["jobs"]["aggregate"]["if"] == "always()", (
        "the aggregate must run even when gates fail, or a failing tier "
        "reports nothing at all"
    )


@pytest.mark.parametrize("workflow", TIER_WORKFLOWS, ids=lambda p: p.stem)
def test_gate_verdict_filenames_survive_artifact_upload(workflow: Path) -> None:
    """`upload-artifact@v4` drops hidden files, and bash `*` skips dotfiles.

    A gate named `.eslintrc` would slugify to a leading-dot filename that is
    excluded from the artifact AND unmatched by the aggregate's glob — the
    verdict vanishes and the count check fails on every run. The `gate-`
    prefix is what prevents it, so assert it is still there on both sides.
    """
    data = load(workflow)
    writes = []
    for step in data["jobs"]["gate"]["steps"]:
        for line in (step.get("run") or "").splitlines():
            if "verdicts/" in line and ">" in line:
                writes.append((step.get("name"), line.strip()))

    assert writes, (
        f"{workflow.name}: found no step writing into verdicts/ — either the "
        "verdict mechanism moved or this assertion has stopped checking anything"
    )
    for step_name, line in writes:
        assert "verdicts/gate-" in line, (
            f"{workflow.name} step {step_name!r} writes a verdict without the "
            f"`gate-` prefix: {line!r}. A leading dot is dropped by "
            "upload-artifact and unmatched by the aggregate's glob, so the "
            "verdict would vanish and every run would fail the count check."
        )


def test_selftest_exercises_every_tier_reusable() -> None:
    """The self-test must actually call all three, or coverage is theatre."""
    data = load(SELFTEST)
    used = {
        job["uses"] for job in data["jobs"].values() if isinstance(job.get("uses"), str)
    }
    for workflow in (FAST, PREPROD, SUMMARY):
        ref = f"./.github/workflows/{workflow.name}"
        assert ref in used, (
            f"{SELFTEST.name} never calls {workflow.name}; it would ship "
            "unexecuted despite a green self-test."
        )
