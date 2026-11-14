#!/usr/bin/env python3
"""Pack-only finalise compatibility: the six-file runner has no QA gate."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "portable_agent_validation_contract", Path(__file__).with_name("test_agent_check.py"))
portable = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(portable)


class FinaliseReadinessCompatibilityTests(unittest.TestCase):
    def assert_actual_runner_receipts(self, method: str, expected: str, observations: int):
        fixture = portable.AgentCheckTests(method)
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        real_receipt = fixture.receipt
        gate = portable.load("qa/finalise-gate")
        observed = []
        seen = set()

        def check_receipt():
            receipt = real_receipt()
            identity = json.dumps(receipt, sort_keys=True)
            if identity not in seen:
                command = [sys.executable, str(fixture.root / "scripts/agent-check.py"),
                           "--provision", "--provider", "codex", "--repo-root", str(fixture.root)]
                real_popen = gate.subprocess.Popen
                launches = []

                def launch(argv, *args, **kwargs):
                    # Only preserve this actual runner's already-produced
                    # receipt. Metadata subprocess.run must execute real Git.
                    if argv == command:
                        launches.append(argv)
                        return SimpleNamespace(wait=lambda **_: 1)
                    return real_popen(argv, *args, **kwargs)

                with mock.patch.object(gate.subprocess, "Popen", side_effect=launch):
                    verdict = gate.run_checks(fixture.root, "codex", 5)
                self.assertEqual(launches, [command])
                self.assertEqual(verdict["outcome"], expected)
                seen.add(identity)
                observed.append(receipt)
            return receipt

        # Run the original concrete runner scenarios and all their assertions.
        # Observe both unknown windows and the known-change upgrade, rather
        # than manufacturing a receipt or making the portable suite optional.
        with mock.patch.object(fixture, "receipt", side_effect=check_receipt):
            getattr(fixture, method)()
        self.assertEqual(len(observed), observations)
        self.assertTrue(all(value["schema"] == "agent-check/v1" for value in observed))

    def test_unknown_postscans_remain_incomplete_to_existing_finalise_reader(self):
        self.assert_actual_runner_receipts(
            "test_unknown_postscan_cannot_reuse_cached_pass_or_claim_source_failure", "incomplete", 2)

    def test_later_known_change_remains_failure_to_existing_finalise_reader(self):
        self.assert_actual_runner_receipts(
            "test_later_known_change_upgrades_an_unknown_postscan_to_source_failure", "fail", 1)


if __name__ == "__main__":
    unittest.main()
