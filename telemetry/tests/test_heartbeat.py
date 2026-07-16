#!/usr/bin/env python3
"""Wire tests for telemetry/heartbeat.py against a fake Sentry SDK.

These pin the exact defects the 2026-07 drift-sentinel iteration cycle
surfaced only at review time (see reports/drift-sentinel-2026-07-12.md and
reports/telemetry-framework-hardening plan):

  * checkin_margin is Sentry's grace AFTER the expected next check-in, not
    total silence — the helper must subtract the tick interval.
  * monitor_config is an upsert — sending it on every tick clobbers
    UI-tuned thresholds; it must go out on the FIRST tick only.
  * canary failure must send status="error" (two-signal principle), while
    a canary failure during shutdown is a benign race and must NOT page.
  * a missing sentry_sdk is a soft-noop, never a crash.

Run: python3 telemetry/tests/test_heartbeat.py (from the plugin root)
"""
from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path

# Plugin installs guarantee CLAUDE_PLUGIN_ROOT at runtime; the __file__ walk is
# the in-tree fallback (source repo / direct checkout).
_PLUGIN_ROOT = os.environ.get("CLAUDE_PLUGIN_ROOT")
_HELPER = (
    Path(_PLUGIN_ROOT) / "telemetry" / "heartbeat.py"
    if _PLUGIN_ROOT
    else Path(__file__).resolve().parent.parent / "heartbeat.py"
)
_spec = importlib.util.spec_from_file_location("heartbeat", _HELPER)
hb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hb)


class FakeCrons:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def capture_checkin(self, **kwargs) -> None:
        self.calls.append(kwargs)


class FakeSentry:
    def __init__(self) -> None:
        self.crons = FakeCrons()
        self.exceptions: list[BaseException] = []

    def capture_exception(self, e: BaseException) -> None:
        self.exceptions.append(e)


class HeartbeatWireTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeSentry()
        self._orig = hb.sentry_sdk
        hb.sentry_sdk = self.fake

    def tearDown(self) -> None:
        hb.sentry_sdk = self._orig

    # --- semantics: operator intent vs provider grace -----------------

    def test_margin_is_target_silence_minus_interval(self):
        h = hb.Heartbeat(surface="s", interval_seconds=300, checkin_margin_minutes=15)
        h._tick()
        cfg = self.fake.crons.calls[0]["monitor_config"]
        # operator asked "page at 15 min silent"; expected next check-in is
        # 5 min out, so Sentry's grace must be 10.
        self.assertEqual(cfg["checkin_margin"], 10)
        self.assertEqual(cfg["schedule"], {"type": "interval", "value": 5, "unit": "minute"})

    def test_default_margin_is_three_intervals_of_silence(self):
        h = hb.Heartbeat(surface="s", interval_seconds=300)
        h._tick()
        cfg = self.fake.crons.calls[0]["monitor_config"]
        # default target silence = 3× interval (15) → grace = 15 - 5 = 10
        self.assertEqual(cfg["checkin_margin"], 10)

    def test_margin_clamps_to_one_when_target_equals_interval(self):
        h = hb.Heartbeat(surface="s", interval_seconds=300, checkin_margin_minutes=5)
        h._tick()
        self.assertEqual(self.fake.crons.calls[0]["monitor_config"]["checkin_margin"], 1)

    def test_beat_once_subtracts_interval_when_given(self):
        hb.beat_once(surface="s", schedule="0 * * * *", checkin_margin_minutes=180, interval_minutes=60)
        cfg = self.fake.crons.calls[0]["monitor_config"]
        self.assertEqual(cfg["checkin_margin"], 120)
        self.assertEqual(cfg["schedule"], {"type": "crontab", "value": "0 * * * *"})

    def test_beat_once_passes_raw_margin_without_interval(self):
        hb.beat_once(surface="s", schedule="0 * * * *", checkin_margin_minutes=15)
        self.assertEqual(self.fake.crons.calls[0]["monitor_config"]["checkin_margin"], 15)

    # --- state model: provisioning + shutdown -------------------------

    def test_monitor_config_sent_on_first_tick_only(self):
        h = hb.Heartbeat(surface="s", interval_seconds=300)
        h._tick()
        h._tick()
        self.assertIn("monitor_config", self.fake.crons.calls[0])
        self.assertNotIn("monitor_config", self.fake.crons.calls[1])

    def test_stop_before_tick_sends_nothing(self):
        h = hb.Heartbeat(surface="s")
        h._stop.set()
        h._tick()
        self.assertEqual(self.fake.crons.calls, [])

    def test_canary_failure_during_shutdown_does_not_page(self):
        h = hb.Heartbeat(surface="s", canary=lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        def failing_canary():
            h._stop.set()  # stop() signalled mid-canary
            raise RuntimeError("boom")

        h.canary = failing_canary
        h._tick()
        self.assertEqual(self.fake.crons.calls, [])
        self.assertEqual(self.fake.exceptions, [])

    # --- two-signal principle ------------------------------------------

    def test_canary_failure_sends_error_status(self):
        def failing_canary():
            raise RuntimeError("pipeline stalled")

        h = hb.Heartbeat(surface="s", canary=failing_canary)
        h._tick()
        self.assertEqual(self.fake.crons.calls[0]["status"], "error")
        self.assertEqual(len(self.fake.exceptions), 1)

    def test_beat_once_canary_failure_sends_error_status(self):
        def failing_canary():
            raise RuntimeError("stalled")

        hb.beat_once(surface="s", canary=failing_canary)
        self.assertEqual(self.fake.crons.calls[0]["status"], "error")

    def test_healthy_tick_sends_ok(self):
        h = hb.Heartbeat(surface="s", canary=lambda: None)
        h._tick()
        self.assertEqual(self.fake.crons.calls[0]["status"], "ok")

    # --- fail-soft contract ----------------------------------------------

    def test_missing_sdk_is_noop(self):
        hb.sentry_sdk = None
        h = hb.Heartbeat(surface="s")
        h._tick()  # must not raise
        hb.beat_once(surface="s")  # must not raise

    def test_broken_capture_is_swallowed(self):
        def broken(**kwargs):
            raise ConnectionError("sentry down")

        self.fake.crons.capture_checkin = broken
        h = hb.Heartbeat(surface="s")
        h._tick()  # must not raise

    def test_failed_send_retries_provisioning_next_tick(self):
        # A failed first send must NOT consume the one-shot monitor_config —
        # the next successful tick still auto-provisions (Gemini high,
        # ai-starter-pack#22).
        calls: list[dict] = []
        state = {"fail": True}

        def flaky(**kwargs):
            if state["fail"]:
                raise ConnectionError("sentry down")
            calls.append(kwargs)

        self.fake.crons.capture_checkin = flaky
        h = hb.Heartbeat(surface="s")
        h._tick()  # fails — provisioning not consumed
        self.assertFalse(h._provisioned)
        state["fail"] = False
        h._tick()  # first successful send carries the config
        self.assertIn("monitor_config", calls[0])
        self.assertTrue(h._provisioned)

    def test_missing_sdk_does_not_consume_provisioning(self):
        hb.sentry_sdk = None
        h = hb.Heartbeat(surface="s")
        h._tick()
        self.assertFalse(h._provisioned)
        hb.sentry_sdk = self.fake
        h._tick()
        self.assertIn("monitor_config", self.fake.crons.calls[0])

    # --- slug composition -----------------------------------------------

    def test_entity_suffix_on_shared_dsn(self):
        h = hb.Heartbeat(surface="kl-sync-worker", entity="personal")
        h._tick()
        self.assertEqual(self.fake.crons.calls[0]["monitor_slug"], "kl-sync-worker-personal")

    def test_manolii_slug_stays_plain(self):
        hb.beat_once(surface="finance-import", entity="manolii")
        self.assertEqual(self.fake.crons.calls[0]["monitor_slug"], "finance-import")

    # --- lifecycle: start/stop thread ------------------------------------

    def test_start_stop_joins_thread(self):
        h = hb.Heartbeat(surface="s", interval_seconds=60)
        h.start()
        h.stop()
        self.assertIsNone(h._thread)
        # first tick fired on start
        self.assertEqual(len(self.fake.crons.calls), 1)


if __name__ == "__main__":
    unittest.main()
