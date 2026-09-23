"""Shared heartbeat helper for Python surfaces (manolii-finance import,
manolii-scrape scheduler, LiteLLM proxy, astroengine background jobs).

Uses Sentry Cron Monitors (``sentry_sdk.crons.capture_checkin``) — the
primitive Sentry designed for exactly this "recurring job liveness" case,
with built-in absent-check-in alerting. Custom metrics were the original
design in ``reports/drift-sentinel-2026-07-12.md`` § 3.1 but were sunset in
Sentry v10; Cron Monitors are the correct replacement (2026-07-16 fix,
PR #2735).

Contract: importer owns ``sentry_sdk.init``; this helper only sends
check-ins. A missing/misconfigured Sentry is a soft-noop — a bad DSN must
never take down the worker it is meant to observe.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Literal, Optional  # noqa: F401 — Literal used below

try:
    import sentry_sdk  # type: ignore[import-untyped]
except ImportError:
    sentry_sdk = None  # type: ignore[assignment]

Entity = Literal["manolii", "personal", "impaktful"]

_MIN_INTERVAL_S = 60  # sub-minute cadence has no detection value
_DEFAULT_INTERVAL_S = 300  # 5 minutes — see report § 3.2


def _monitor_slug(surface: str, entity: Entity = "manolii") -> str:
    """Compose the Sentry monitor slug. Manolii is the default entity so its
    slugs stay plain; personal/impaktful get suffixed to keep monitors
    distinct even though they share Manolii's DSN (report § 3.4).
    """
    return surface if entity == "manolii" else f"{surface}-{entity}"


def _capture_checkin(
    monitor_slug: str,
    monitor_config: Optional[dict[str, Any]] = None,
    status: Literal["ok", "error"] = "ok",
) -> bool:
    """Send a check-in for ``monitor_slug``. All error paths swallow —
    a broken sentry_sdk must never crash the surface being observed.
    ``status="error"`` is used when the caller's canary detected a stalled
    pipeline (two-signal principle, report § 3).

    Returns True only when the capture call was actually dispatched without
    raising — callers use this to retry monitor auto-provisioning on the next
    tick instead of burning the one-shot config on a failed send (Gemini high,
    ai-starter-pack#22). NOTE: no get_client()/Hub introspection here — those
    semantics vary across sentry-sdk majors (2.x returns a NonRecordingClient
    when uninitialised, not None); the contract remains importer-owns-init.
    """
    if sentry_sdk is None:
        return False
    try:
        crons = getattr(sentry_sdk, "crons", None)
        capture = getattr(crons, "capture_checkin", None) if crons else None
        if capture is None:
            return False
        if monitor_config is not None:
            capture(monitor_slug=monitor_slug, status=status, monitor_config=monitor_config)
        else:
            capture(monitor_slug=monitor_slug, status=status)
        return True
    except Exception:  # noqa: BLE001
        return False


class Heartbeat:
    """Long-lived heartbeat. Start once, ``stop()`` on shutdown.

    Example::

        from scripts.lib.heartbeat import Heartbeat
        hb = Heartbeat(surface="finance-import", entity="manolii")
        hb.start()
        try:
            run_import_loop()
        finally:
            hb.stop()
    """

    def __init__(
        self,
        *,
        surface: str,
        entity: Entity = "manolii",
        interval_seconds: int = _DEFAULT_INTERVAL_S,
        canary: Optional[Callable[[], None]] = None,
        checkin_margin_minutes: Optional[int] = None,
        max_runtime_minutes: int = 5,
    ) -> None:
        self.surface = surface
        self.entity = entity
        self.interval = max(_MIN_INTERVAL_S, interval_seconds)
        self.canary = canary
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        interval_minutes = max(1, round(self.interval / 60))
        # checkin_margin_minutes is the operator's INTENDED "N minutes silent
        # = page" threshold (report § 5). Sentry's checkin_margin, however,
        # is the grace AFTER the expected next check-in — not total silence
        # from the last one. Subtract the tick interval so an operator who
        # asks for "page at 15 min silent" actually gets alerted at 15 min
        # of silence (Sentry expected=5min + grace=10min = 15min), not at
        # 20 min. Clamp to 1 so a target equal to the interval still pages
        # promptly after the expected time. (CodeRabbit major, KL#637.)
        target_silence = checkin_margin_minutes if checkin_margin_minutes is not None else max(3, interval_minutes * 3)
        sentry_margin = max(1, target_silence - interval_minutes)
        self._monitor_config: dict[str, Any] = {
            "schedule": {"type": "interval", "value": interval_minutes, "unit": "minute"},
            "checkin_margin": sentry_margin,
            "max_runtime": max_runtime_minutes,
        }
        self._monitor_slug = _monitor_slug(self.surface, self.entity)
        # Sentry treats monitor_config as an upsert — sending it on every tick
        # would overwrite any UI-tuned threshold back to our coded default,
        # so operators lose the ability to widen the >Nh silence window.
        # Send config on the FIRST tick only (auto-provisioning) then omit;
        # Sentry keeps whatever config was last written (UI or code).
        self._provisioned = False

    def _tick(self, run_canary: bool = True) -> None:
        # Shutdown short-circuit: skip the entire tick if stop was already
        # called. Prevents a spurious error check-in when an in-flight tick
        # collides with graceful shutdown (Codex P2 on scrape#70).
        if self._stop.is_set():
            return
        # Two-signal principle (report § 3): heartbeat alone catches "process
        # died"; canary catches "process alive, pipeline broken". If the
        # canary raises, we send status=error so Sentry pages immediately
        # instead of waiting for the >Nh silence threshold (which will never
        # trip while the process keeps ticking ok's).
        status: Literal["ok", "error"] = "ok"
        if self.canary and run_canary:
            try:
                self.canary()
            except Exception as e:  # noqa: BLE001 — canary failure is exactly what we want to capture
                # Second short-circuit: if stop() was signalled DURING the
                # canary (e.g. FastAPI lifespan called stop() and the
                # canary's run_coroutine_threadsafe timed out because the
                # loop is now blocked on join()), that's a benign shutdown
                # race, not a real pipeline failure. Don't page.
                if not self._stop.is_set():
                    status = "error"
                    if sentry_sdk is not None:
                        try:
                            sentry_sdk.capture_exception(e)
                        except Exception:  # noqa: BLE001
                            pass
        # Also skip the check-in send once stop was signalled — no need to
        # emit a final "ok" that could confuse deploy-window diagnostics.
        if self._stop.is_set():
            return
        sent = _capture_checkin(
            self._monitor_slug,
            self._monitor_config if not self._provisioned else None,
            status=status,
        )
        if sent:
            # Only consume the one-shot provisioning config on a dispatched
            # send — a missing SDK or send failure retries config next tick.
            self._provisioned = True

    def _loop(self) -> None:
        # start() already sent a synchronous canary-LESS boot check-in. Run
        # one immediate tick WITH the canary here, off the caller's thread,
        # so a broken pipeline is detected at boot rather than one interval
        # later (Gemini critical, scrape#74).
        self._tick()
        while not self._stop.wait(self.interval):
            self._tick()

    def start(self) -> None:
        if self._thread is not None:
            return
        # Clear so a stopped Heartbeat can be restarted (test/dynamic reload).
        self._stop.clear()
        # Fire the first tick SYNCHRONOUSLY before spawning the thread, so a
        # surface that boots and dies (or stops immediately) is deterministically
        # counted as "was here" — an async first tick loses the race when
        # stop() lands before the daemon thread is scheduled (Codex P2,
        # ai-starter-pack#22). The canary is SKIPPED on this one tick: start()
        # is typically called from the app's event-loop thread (FastAPI
        # lifespan), and an async-bridging canary (run_coroutine_threadsafe +
        # blocking result) can never complete while that thread is blocked
        # here — it would stall boot and page a false canary failure on every
        # deploy (Gemini critical, scrape#74). The daemon thread's first tick
        # (immediate, in _loop) runs the canary off-thread instead.
        self._tick(run_canary=False)
        self._thread = threading.Thread(
            target=self._loop, name=f"heartbeat[{self.surface}]", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        # Short join: the loop wakes on _stop.set() immediately, but the
        # canary/network call inside a tick may still be in flight. 5s is
        # generous enough for clean exit AND short enough to leave Fly's
        # ~15-30s SIGTERM grace period for the rest of the shutdown path.
        # Daemon thread will die with the process if it doesn't join.
        if t is not None:
            t.join(timeout=5.0)
        self._thread = None


def beat_once(
    *,
    surface: str,
    entity: Entity = "manolii",
    canary: Optional[Callable[[], None]] = None,
    schedule: Optional[str] = None,
    checkin_margin_minutes: int = 15,
    max_runtime_minutes: int = 30,
    interval_minutes: Optional[int] = None,
) -> None:
    """One-shot check-in for scheduled/serverless surfaces.

    ``schedule`` is a crontab string. Providing it lets Sentry auto-provision
    the monitor with the correct expected cadence so "missed" detection works.
    Omit only if the monitor is provisioned in the Sentry UI.

    ``checkin_margin_minutes`` is the operator's "N minutes silent = page"
    target. Pass ``interval_minutes`` matching the cron's cadence (e.g. 60
    for hourly) so the helper can subtract it — Sentry's checkin_margin is
    grace AFTER the expected check-in, not total silence. Without
    interval_minutes, the value passes through as Sentry's raw grace.

    CONFIG IS CODE-OWNED for beat_once surfaces: each invocation is a fresh
    serverless process with no provisioning state, so ``monitor_config`` is
    (re)asserted on every run — Sentry's own documented cron pattern. Tune
    thresholds for these surfaces in CODE, not the Sentry UI; UI edits are
    overwritten on the next run. (Long-lived ``Heartbeat`` surfaces are the
    opposite: config on the first check-in only, UI tuning respected.)
    """
    # Two-signal principle (report § 3): canary failure ⇒ error check-in so
    # Sentry pages immediately instead of relying on the >Nh silence window.
    status: Literal["ok", "error"] = "ok"
    if canary:
        try:
            canary()
        except Exception as e:  # noqa: BLE001 — canary failure is exactly the signal
            status = "error"
            if sentry_sdk is not None:
                try:
                    sentry_sdk.capture_exception(e)
                except Exception:  # noqa: BLE001
                    pass
    monitor_config: Optional[dict[str, Any]] = None
    if schedule is not None:
        sentry_margin = checkin_margin_minutes
        if interval_minutes is not None:
            sentry_margin = max(1, checkin_margin_minutes - interval_minutes)
        monitor_config = {
            "schedule": {"type": "crontab", "value": schedule},
            "checkin_margin": sentry_margin,
            "max_runtime": max_runtime_minutes,
        }
    _capture_checkin(_monitor_slug(surface, entity), monitor_config, status=status)
