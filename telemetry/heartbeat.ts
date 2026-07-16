// Shared heartbeat helper for TypeScript/Node surfaces (kl-sync-worker,
// KL cron routes, manolii-platform, impaktful_3.0).
//
// Uses Sentry Cron Monitors (`captureCheckIn`) — the primitive Sentry
// designed for exactly this "recurring job liveness" case, with built-in
// absent-check-in alerting. Custom metrics were the original design in
// reports/drift-sentinel-2026-07-12.md § 3.1 but were sunset in Sentry v10;
// Cron Monitors are the correct replacement (2026-07-16 fix, PR #2735).
//
// Contract: importer owns Sentry init; this helper only sends check-ins.
// A missing/uninitialised Sentry is a soft-noop (never throws) — a bad DSN
// must never take down the worker it is meant to observe.

/**
 * Structural type for @sentry/node and @sentry/nextjs. `captureCheckIn`'s
 * monitor-config parameter is deliberately loose (`any`, see inline note) so
 * the actual Sentry SDK — whichever version the consumer has installed —
 * validates it at runtime. Typing it strictly here would fight minor Sentry
 * API drift.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type SentryLike = {
  // monitorConfig is typed `any` (not `unknown`) so the real Sentry SDK's
  // strict `MonitorConfig | undefined` is assignable under strict function
  // parameter variance — `unknown` is a supertype and thus rejected. We
  // construct the object with a well-typed literal at each call site;
  // Sentry validates the shape at runtime.
  captureCheckIn?: (
    checkIn: {
      monitorSlug: string;
      status: "ok" | "error";
      checkInId?: string;
      duration?: number;
    },
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    monitorConfig?: any,
  ) => string | undefined;
  captureException?: (e: unknown) => void;
};

type Entity = "manolii" | "personal" | "impaktful";

/** Compose the Sentry monitor slug. Manolii is the default entity so its
 * slugs stay plain; personal/impaktful get suffixed to keep monitors distinct
 * even though they share Manolii's DSN (report § 3.4). */
function monitorSlugFor(surface: string, entity: Entity = "manolii"): string {
  return entity === "manolii" ? surface : `${surface}-${entity}`;
}

export type HeartbeatOpts = {
  /** Stable name for the surface, e.g. "kl-sync-worker", "inbox-briefing". */
  surface: string;
  /** Entity tag: manolii | personal | impaktful. */
  entity?: Entity;
  /** How often to emit. Default 5 minutes.
   *  Do NOT set below 60s — sub-minute cadence has no detection value. */
  intervalMs?: number;
  /** Pre-initialised Sentry SDK. Pass @sentry/node or @sentry/nextjs. */
  sentry: SentryLike;
  /** Optional callback invoked once per tick before the check-in.
   *  Use for a canary payload (report § 3 two-signal principle). Errors
   *  captured and swallowed — the heartbeat still fires. */
  canary?: () => Promise<void> | void;
  /** The operator's INTENDED silence threshold for this surface, per
   *  master/reports/drift-sentinel-2026-07-12.md § 5. Encoded in code so a
   *  deploy re-provisions the Sentry monitor with the documented threshold
   *  instead of collapsing to a short default. Omit to get 3× interval. */
  checkinMarginMinutes?: number;
  /** Maximum expected run duration in minutes (Sentry pages if a check-in's
   *  in_progress lasts longer). Default 5. */
  maxRuntimeMinutes?: number;
};

export type HeartbeatHandle = {
  /** Stop the timer. Idempotent. */
  stop: () => void;
  /** Fire one tick synchronously — useful for tests. */
  tick: () => Promise<void>;
};

/**
 * Start a long-lived heartbeat for `surface`. Every `intervalMs` sends a
 * `status: ok` check-in with an interval-based monitor config so Sentry
 * auto-provisions the monitor. Returns a disposer for graceful shutdown.
 *
 * @example
 *   import { startHeartbeat } from "./lib/heartbeat";
 *   import * as Sentry from "@sentry/node";
 *   const hb = startHeartbeat({ surface: "kl-sync-worker", entity: "manolii", sentry: Sentry });
 *   process.on("SIGTERM", () => hb.stop());
 */
export function startHeartbeat(opts: HeartbeatOpts): HeartbeatHandle {
  const interval = Math.max(60_000, opts.intervalMs ?? 5 * 60_000);
  const intervalMinutes = Math.round(interval / 60_000);
  const monitorSlug = monitorSlugFor(opts.surface, opts.entity);
  // Sentry accepts crontab or interval; interval fits recurring liveness.
  // checkinMargin generous enough to survive one legitimate stall (3× interval)
  // without paging; SREs tighten thresholds in Sentry UI after baseline.
  // checkinMarginMinutes is the operator's "N min silent = page" target
  // (report § 5). Sentry's checkinMargin is grace AFTER the expected next
  // check-in, not total silence from the last. Subtract the tick interval
  // so "page at 15 min silent" actually pages at 15 min. Clamp to 1 so a
  // target equal to the interval still fires promptly after expected.
  // (CodeRabbit major, KL#637.)
  const targetSilence = opts.checkinMarginMinutes ?? Math.max(3, intervalMinutes * 3);
  const sentryMargin = Math.max(1, targetSilence - intervalMinutes);
  const monitorConfig = {
    schedule: { type: "interval" as const, value: intervalMinutes, unit: "minute" as const },
    checkinMargin: sentryMargin,
    maxRuntime: opts.maxRuntimeMinutes ?? 5,
  };

  // Sentry treats monitor_config as an upsert — sending it on every tick
  // would overwrite any UI-tuned threshold back to our coded default. Send
  // config on the FIRST tick only (auto-provisioning) then omit; Sentry keeps
  // whatever config was last written (UI or code).
  let provisioned = false;

  let stopped = false;

  const tick = async () => {
    // Shutdown short-circuit: skip entirely if stop() was called (e.g.
    // graceful process shutdown collided with an in-flight interval).
    if (stopped) return;
    // Two-signal principle (report § 3): canary failure ⇒ error check-in
    // so Sentry pages immediately instead of waiting for the >Nh silence
    // window, which would never trip while the process keeps ticking ok's.
    let status: "ok" | "error" = "ok";
    if (opts.canary) {
      try {
        await opts.canary();
      } catch (e) {
        // If stop() was signalled DURING the canary, treat as shutdown
        // race not real failure — don't page.
        if (!stopped) {
          status = "error";
          try {
            opts.sentry.captureException?.(e);
          } catch {
            // fail-soft: a broken captureException must not break the tick
          }
        }
      }
    }
    if (stopped) return;
    try {
      // Only consume the one-shot provisioning config on a dispatched send —
      // if captureCheckIn is absent (SDK not wired) or throws, retry the
      // config next tick (Gemini high, ai-starter-pack#22). No getClient()
      // introspection: init-state semantics vary across Sentry SDK majors;
      // the contract remains importer-owns-init.
      if (opts.sentry.captureCheckIn) {
        opts.sentry.captureCheckIn(
          { monitorSlug, status },
          provisioned ? undefined : monitorConfig,
        );
        provisioned = true;
      }
    } catch {
      // never let a telemetry failure crash the surface we are meant to watch
    }
  };

  // Fire immediately so a surface that boots and dies within intervalMs is
  // still counted as "was here." Then repeat.
  void tick();
  const timer = setInterval(tick, interval);
  // Node: don't hold the event loop open just for the heartbeat — the surface
  // itself must decide when to exit. In Edge/browser environments `unref`
  // doesn't exist; duck-type without a Node types dependency.
  const timerObj = timer as unknown as { unref?: () => void };
  if (typeof timerObj.unref === "function") {
    timerObj.unref();
  }

  return {
    stop: () => {
      if (stopped) return;
      stopped = true;
      clearInterval(timer);
    },
    tick,
  };
}

export type BeatOnceOpts = {
  surface: string;
  entity?: Entity;
  sentry: SentryLike;
  canary?: () => Promise<void> | void;
  /** Crontab pattern for the underlying cron. Sentry auto-provisions the
   *  monitor with this schedule so it knows when the next check-in is due.
   *  Omit to send a status check-in without provisioning schedule metadata
   *  (Sentry will still track the monitor but "missed" detection needs the
   *  schedule; set it in the UI later if you skip this). */
  schedule?: string;
  /** Operator's "N minutes silent = page" target. Pass with
   *  `intervalMinutes` (see below) so the helper can convert to Sentry's
   *  grace-after-expected semantic. Without `intervalMinutes`, this value
   *  passes through directly as Sentry's raw grace. Default 15. */
  checkinMarginMinutes?: number;
  /** Cron cadence in minutes (e.g. 60 for hourly, 1440 for daily). When
   *  set, helper subtracts from checkinMarginMinutes to preserve the
   *  operator's "N min silent" intent per Sentry's monitor semantics. */
  intervalMinutes?: number;
  /** Max minutes the run should take. Default 30. */
  maxRuntimeMinutes?: number;
};

/**
 * One-shot check-in for serverless (Vercel cron) surfaces where a long-lived
 * timer makes no sense. Call at the top of the handler; Sentry pages when
 * the next expected check-in doesn't arrive within checkinMarginMinutes.
 *
 * @example
 *   export async function GET() {
 *     await beatOnce({
 *       surface: "kl-inbox-briefing",
 *       sentry: Sentry,
 *       schedule: "0 * * * *",              // top of each hour
 *       checkinMarginMinutes: 180,          // page if >3h silent…
 *       intervalMinutes: 60,                // …after subtracting the cadence
 *     });
 *     // ...handler body...
 *   }
 */
export async function beatOnce(opts: BeatOnceOpts): Promise<void> {
  const monitorSlug = monitorSlugFor(opts.surface, opts.entity);
  // Two-signal principle (report § 3): canary failure ⇒ error check-in.
  let status: "ok" | "error" = "ok";
  if (opts.canary) {
    try {
      await opts.canary();
    } catch (e) {
      status = "error";
      try {
        opts.sentry.captureException?.(e);
      } catch {
        // fail-soft: a broken captureException must not break the handler
      }
    }
  }
  const rawMargin = opts.checkinMarginMinutes ?? 15;
  const sentryMargin = opts.intervalMinutes !== undefined
    ? Math.max(1, rawMargin - opts.intervalMinutes)
    : rawMargin;
  const monitorConfig = opts.schedule
    ? {
        schedule: { type: "crontab" as const, value: opts.schedule },
        checkinMargin: sentryMargin,
        maxRuntime: opts.maxRuntimeMinutes ?? 30,
      }
    : undefined;
  try {
    opts.sentry.captureCheckIn?.({ monitorSlug, status }, monitorConfig);
  } catch {
    // swallow — never fail the handler on a telemetry error
  }
}
