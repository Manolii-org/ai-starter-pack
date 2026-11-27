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
        self.scale = {"run_id": 12, "run_attempt": 1, "id": 13, "name": "Scale sample", "conclusion": "success",
                      "started_at": "2026-11-27T00:00:10Z", "completed_at": "2026-11-27T00:00:30Z"}
        self.scale_run = {"path": ".github/workflows/fleet-scale.yml", "event": "schedule", "head_branch": "main",
                          "repository": {"full_name": "Platform/fleet"},
                          "created_at": "2026-11-27T00:00:09Z"}
        self.logs = (
            "demand=2 (busy=1+queued=1; probed=1, forwarded_floor=0, applied_floor=0) "
            "desired=2 started=1 cap=3 [up: start-only] queue_probe_complete=true\n"
            "2026-11-27T00:00:20.123Z Starting machine abc123 (1/1)\nabc123 has been started\n"
        )

    def evidence(self, scenario="missed-event", wake=None, target=300):
        return wake_acceptance.validate("Example/product", "platform/fleet", "sample", scenario, target, self.job,
                                        self.scale, self.scale_run, self.logs, wake)

    def test_scheduled_fallback(self):
        receipt = self.evidence()
        self.assertEqual(receipt["wake_source"], "scheduled")
        self.assertEqual(receipt["wait_seconds"], 20)

    def test_manual_retry_of_schedule_is_diagnostic(self):
        self.scale["run_attempt"] = 2
        with self.assertRaisesRegex(ValueError, "first attempt"):
            self.evidence()
        self.assertEqual(self.evidence("busy")["wake_source"], "manual")

    def test_cold_event_wake(self):
        self.scale_run["event"] = "workflow_dispatch"
        self.logs = self.logs.replace("busy=1", "busy=0").replace(" started=1", " started=0")
        wake = ("Example/broker", {**self.scale, "started_at": "2026-11-27T00:00:04Z"},
                {"path": wake_acceptance.WAKE_WORKFLOW, "event": "workflow_run"},
                'wake\tdispatch\t2026-11-27T00:00:09Z {"dispatched": true, "dry_run": false, "fleet_dispatch": '
                '{"repository": "Platform/fleet", "tenant": "sample", "action": "up", "run_id": 12}}')
        receipt = self.evidence("cold", wake)
        self.assertEqual(receipt["wake_source"], "event")
        self.assertIn("github.com/Platform/fleet/", receipt["scale_job"])
        self.assertEqual(self.evidence("cold", (*wake[:3], wake[3].replace("Platform/fleet", "PLATFORM/FLEET"))),
                         receipt)
        with self.assertRaisesRegex(ValueError, "event-triggered"):
            self.evidence("cold", (*wake[:2], {**wake[2], "event": "schedule"}, wake[3]))
        with self.assertRaisesRegex(ValueError, "same organization"):
            self.evidence("cold", ("Other/broker", *wake[1:]))
        with self.assertRaisesRegex(ValueError, "selected fleet run"):
            self.evidence("cold", (*wake[:3], wake[3].replace('"run_id": 12', '"run_id": 99')))
        with self.assertRaisesRegex(ValueError, "first attempts"):
            self.evidence("cold", (wake[0], {**wake[1], "run_attempt": 2}, *wake[2:]))
        self.scale["run_attempt"] = 2
        with self.assertRaisesRegex(ValueError, "first attempts"):
            self.evidence("cold", wake)

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
