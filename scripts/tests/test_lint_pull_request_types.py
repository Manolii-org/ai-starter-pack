#!/usr/bin/env python3
"""Fixture tests for scripts/lint_pull_request_types.py."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "lint_pull_request_types",
    Path(__file__).resolve().parents[1] / "lint_pull_request_types.py",
)
assert _SPEC and _SPEC.loader
LINT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(LINT)


class LintPullRequestTypesTests(unittest.TestCase):
    def _write(self, body: str) -> Path:
        tmp = Path(tempfile.mkdtemp())
        wf = tmp / ".github" / "workflows"
        wf.mkdir(parents=True)
        target = wf / "sample.yml"
        target.write_text(body)
        return tmp

    def test_explicit_types_missing_ready_for_review_fails(self):
        root = self._write(
            "on:\n  pull_request:\n    types: [opened, synchronize]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps: [{run: echo hi}]\n"
        )
        findings = LINT.lint_file(root / ".github/workflows/sample.yml", root)
        self.assertTrue(any("ready_for_review" in f for f in findings))

    def test_explicit_types_with_ready_for_review_clean(self):
        root = self._write(
            "on:\n  pull_request:\n    types: [opened, synchronize, reopened, ready_for_review]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps: [{run: echo hi}]\n"
        )
        self.assertEqual(LINT.lint_file(root / ".github/workflows/sample.yml", root), [])

    def test_implicit_types_not_required(self):
        root = self._write("on: pull_request\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps: [{run: echo hi}]\n")
        self.assertEqual(LINT.lint_file(root / ".github/workflows/sample.yml", root), [])

    def test_pnpm_dev_without_preview_ready_fails(self):
        root = self._write(
            "on:\n  pull_request:\njobs:\n  e2e:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pnpm dev\n"
        )
        findings = LINT.lint_file(root / ".github/workflows/sample.yml", root)
        self.assertTrue(any("pnpm dev" in f for f in findings))

    def test_readonly_needs_mutating_fails(self):
        root = self._write(
            """
on: pull_request
jobs:
  mutating:
    runs-on: ubuntu-latest
    steps: [{run: echo mut}]
  readonly:
    needs: mutating
    runs-on: ubuntu-latest
    steps: [{run: echo ro}]
"""
        )
        findings = LINT.lint_file(root / ".github/workflows/sample.yml", root)
        self.assertTrue(any("readonly" in f and "mutating" in f for f in findings))


if __name__ == "__main__":
    unittest.main()
