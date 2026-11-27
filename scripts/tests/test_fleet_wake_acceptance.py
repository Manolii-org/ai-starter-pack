import copy
import importlib.util
import unittest
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "wake_acceptance", Path(__file__).resolve().parents[1] / "check-fleet-wake-acceptance.py"
)
wake_acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wake_acceptance)


class WakeEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.job = {"run_id": 10, "id": 11, "labels": ["FLY"], "runner_name": "fly-abc123", "conclusion": "success",
                    "created_at": "2026-11-27T00:00:00Z", "started_at": "2026-11-27T00:00:20Z"}
        self.scale = {"run_id": 12, "id": 13, "name": "Scale sample", "conclusion": "success",
                      "started_at": "2026-11-27T00:00:10Z", "completed_at": "2026-11-27T00:00:30Z"}
        self.scale_run = {"path": ".github/workflows/fleet-scale.yml", "event": "schedule", "head_branch": "main",
                          "created_at": "2026-11-27T00:00:09Z"}
        self.logs = (
            "demand=2 (busy=1+queued=1; probed=1, forwarded_floor=0, applied_floor=0) "
            "desired=2 started=1 cap=3 [up: start-only] queue_probe_complete=true\n"
            "2026-11-27T00:00:20.123Z Starting machine abc123 (1/1)\nabc123 has been started\n"
        )

    def evidence(self, scenario="missed-event", wake=None, target=300):
        return wake_acceptance.validate("Example/product", "Platform/fleet", "sample", scenario, target, self.job,
                                        self.scale, self.scale_run, self.logs, wake)

    def test_scheduled_fallback(self):
        receipt = self.evidence()
        self.assertEqual(receipt["wake_source"], "scheduled")
        self.assertEqual(receipt["wait_seconds"], 20)

    def test_cold_event_wake(self):
        self.scale_run["event"] = "workflow_dispatch"
        self.logs = self.logs.replace("busy=1", "busy=0").replace(" started=1", " started=0")
        wake = ("Example/broker", {**self.scale, "started_at": "2026-11-27T00:00:04Z"},
                {"path": wake_acceptance.WAKE_WORKFLOW, "event": "workflow_run"},
                '{"dispatched": true, "dry_run": false}')
        self.assertEqual(self.evidence("cold", wake)["wake_source"], "event")
        with self.assertRaisesRegex(ValueError, "same organization"):
            self.evidence("cold", ("Other/broker", *wake[1:]))

    def test_needs_real_compatible_assignment_and_complete_demand(self):
        original = copy.deepcopy(self.job)
        self.job["runner_name"] = "fly-def456"
        with self.assertRaisesRegex(ValueError, "assigned Machine"):
            self.evidence()
        self.job = original
        self.logs = self.logs.replace("probed=1", "probed=0")
        with self.assertRaisesRegex(ValueError, "real queued work"):
            self.evidence()

    def test_manual_wake_is_diagnostic_and_target_is_enforced(self):
        self.scale_run["event"] = "workflow_dispatch"
        self.assertEqual(self.evidence("busy")["wake_source"], "manual")
        with self.assertRaisesRegex(ValueError, "scheduled fleet run"):
            self.evidence()
        with self.assertRaisesRegex(ValueError, "pickup took"):
            self.evidence("busy", target=19)


if __name__ == "__main__":
    unittest.main()
