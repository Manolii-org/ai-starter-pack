"""Evaluate the actual standalone selectors without services or live variables."""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/pr-assessment.yml"
JOBS = ("classify", "specialists", "broad-agents", "judge")
PACK = "Manolii-org/ai-starter-pack"
FLY = ["self-hosted", "fly"]
pytestmark = pytest.mark.skipif(not WORKFLOW.exists(), reason="standalone workflow absent")


def _truth(value):
    # Actions arrays/objects, including empty ones, are truthy.
    return not (value is None or value is False or value == "" or value == 0)


def _evaluate(expression, context):
    """Small fail-closed interpreter for the selector's Actions expression subset.

    Parse the YAML value itself, preserving short-circuit and operand-return
    semantics. No selector decision is reimplemented here, and unknown syntax
    fails the test rather than being silently ignored. Contexts use the actual
    string types of repository_id/vars and missing properties return ''.
    """
    assert expression.startswith("${{") and expression.endswith("}}")
    expression = expression[3:-2].strip()
    # Rewrite operators only outside quoted Actions literals (JSON uses quotes).
    parts = re.split(r"('(?:[^']|'')*')", expression)
    expression = "".join(
        part if i % 2 else part.replace("&&", " and ").replace("||", " or ")
        for i, part in enumerate(parts)
    )

    def property_path(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return property_path(node.value) + "." + node.attr
        raise AssertionError(ast.dump(node))

    def visit(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.Name, ast.Attribute)):
            return context.get(property_path(node), "")
        if isinstance(node, ast.BoolOp):
            for item in node.values:
                value = visit(item)
                if isinstance(node.op, ast.And) and not _truth(value):
                    return value
                if isinstance(node.op, ast.Or) and _truth(value):
                    return value
            return value
        if isinstance(node, ast.Compare):
            assert len(node.ops) == 1 and len(node.comparators) == 1
            left, right = visit(node.left), visit(node.comparators[0])
            # These selectors compare strings only; Actions ignores case.
            assert isinstance(left, str) and isinstance(right, str)
            equal = left.casefold() == right.casefold()
            assert isinstance(node.ops[0], (ast.Eq, ast.NotEq))
            return equal if isinstance(node.ops[0], ast.Eq) else not equal
        if isinstance(node, ast.Call):
            assert isinstance(node.func, ast.Name) and node.func.id == "fromJSON"
            assert len(node.args) == 1 and not node.keywords
            return json.loads(visit(node.args[0]))
        raise AssertionError(ast.dump(node))

    return visit(ast.parse(f"({expression})", mode="eval").body)


def _context(**changes):
    context = {
        "github.event_name": "pull_request",
        "github.repository": PACK,
        "github.repository_id": "1268261727",
        "github.event.repository.visibility": "public",
        "github.event.pull_request.head.repo.full_name": PACK,
    }
    context.update(changes)
    return context


@pytest.fixture(params=JOBS)
def selector(request):
    return yaml.safe_load(WORKFLOW.read_text())["jobs"][request.param]["runs-on"]


@pytest.mark.parametrize("override,expected", [
    (None, ["ubuntu-latest"]), ("", ["ubuntu-latest"]),
    ('["self-hosted","fly"]', FLY), ('["ubuntu-latest"]', ["ubuntu-latest"]),
    ('"ubuntu-latest"', "ubuntu-latest"),
])
def test_public_pack_and_override_precedence(selector, override, expected):
    context = _context()
    if override is not None:
        context["vars.CI_RUNNER_OVERRIDE"] = override
    assert _evaluate(selector, context) == expected


@pytest.mark.parametrize("changes", [
    {"github.repository_id": "1"},
    {"github.repository_id": ""},
    {"github.repository": "example/copied-consumer",
     "github.event.pull_request.head.repo.full_name": "example/copied-consumer"},
    {"github.repository": "Manolii-org/renamed-pack",
     "github.event.pull_request.head.repo.full_name": "Manolii-org/renamed-pack"},
    {"github.repository": "example/ai-starter-pack",
     "github.event.pull_request.head.repo.full_name": "example/ai-starter-pack"},
    {"github.event.repository.visibility": "private"},
    {"github.event.repository.visibility": "internal"},
    {"github.event.repository.visibility": ""},
    {"github.event.repository.visibility": "unknown"},
])
def test_unqualified_identity_retains_fly(selector, changes):
    context = _context(**changes)
    assert _evaluate(selector, context) == FLY
    context["vars.CI_RUNNER_OVERRIDE"] = '["ubuntu-latest"]'
    assert _evaluate(selector, context) == ["ubuntu-latest"]


@pytest.mark.parametrize("missing", ["github.repository_id", "github.event.repository.visibility"])
def test_absent_identity_is_conservative(selector, missing):
    context = _context()
    del context[missing]
    assert _evaluate(selector, context) == FLY


@pytest.mark.parametrize("override", [None, '["self-hosted","fly"]', "invalid-json"])
def test_fork_guard_short_circuits_even_invalid_override(selector, override):
    context = _context(**{"github.event.pull_request.head.repo.full_name": "example/fork"})
    if override is not None:
        context["vars.CI_RUNNER_OVERRIDE"] = override
    assert _evaluate(selector, context) == "ubuntu-latest"


def test_invalid_override_still_fails(selector):
    with pytest.raises(json.JSONDecodeError):
        _evaluate(selector, _context(**{"vars.CI_RUNNER_OVERRIDE": "invalid-json"}))


@pytest.mark.parametrize("override,expected", [
    ("[]", []), ("null", "ubuntu-latest"), ("false", "ubuntu-latest"),
    ("0", "ubuntu-latest"), ('""', "ubuntu-latest"),
])
def test_existing_json_truthiness_behavior(selector, override, expected):
    assert _evaluate(selector, _context(**{"vars.CI_RUNNER_OVERRIDE": override})) == expected


def test_four_selectors_share_one_policy_and_sast_is_unchanged():
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    assert len({jobs[job]["runs-on"] for job in JOBS}) == 1
    assert jobs["sast"]["runs-on"] == "ubuntu-latest"
