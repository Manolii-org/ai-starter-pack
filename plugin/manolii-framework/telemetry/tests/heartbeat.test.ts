// Wire tests for telemetry/heartbeat.ts against a fake Sentry SDK.
// Runner: vitest (any TS test runner with describe/it/expect works).
// These pin the exact defects the 2026-07 drift-sentinel iteration surfaced
// only at review time — see the docstring in tests/test_heartbeat.py (the
// Python twin) for the full defect catalogue.

import { describe, it, expect } from "vitest";
import { startHeartbeat, beatOnce } from "../heartbeat";

type Call = { checkIn: { monitorSlug: string; status: string }; monitorConfig?: unknown };

function fakeSentry() {
  const calls: Call[] = [];
  const exceptions: unknown[] = [];
  return {
    calls,
    exceptions,
    captureCheckIn: (checkIn: Call["checkIn"], monitorConfig?: unknown) => {
      calls.push({ checkIn, monitorConfig });
      return undefined;
    },
    captureException: (e: unknown) => {
      exceptions.push(e);
    },
  };
}

describe("startHeartbeat wire semantics", () => {
  it("converts operator silence target to Sentry grace (target − interval)", async () => {
    const sentry = fakeSentry();
    const hb = startHeartbeat({
      surface: "s",
      sentry,
      intervalMs: 5 * 60_000,
      checkinMarginMinutes: 15,
    });
    hb.stop();
    // first tick fired synchronously on start
    const cfg = sentry.calls[0].monitorConfig as { checkinMargin: number; schedule: unknown };
    expect(cfg.checkinMargin).toBe(10); // page at 15 min silent = 5 expected + 10 grace
    expect(cfg.schedule).toEqual({ type: "interval", value: 5, unit: "minute" });
  });

  it("sends monitorConfig on the FIRST tick only (Sentry upsert clobbers UI tuning)", async () => {
    const sentry = fakeSentry();
    const hb = startHeartbeat({ surface: "s", sentry, intervalMs: 60_000 });
    await hb.tick();
    hb.stop();
    expect(sentry.calls[0].monitorConfig).toBeDefined();
    expect(sentry.calls[1].monitorConfig).toBeUndefined();
  });

  it("canary failure sends status=error (two-signal principle)", async () => {
    const sentry = fakeSentry();
    const hb = startHeartbeat({
      surface: "s",
      sentry,
      canary: () => {
        throw new Error("pipeline stalled");
      },
    });
    hb.stop();
    expect(sentry.calls[0].checkIn.status).toBe("error");
    expect(sentry.exceptions).toHaveLength(1);
  });

  it("stop() before a tick suppresses the check-in (shutdown race)", async () => {
    const sentry = fakeSentry();
    const hb = startHeartbeat({ surface: "s", sentry, intervalMs: 60_000 });
    const before = sentry.calls.length;
    hb.stop();
    await hb.tick();
    expect(sentry.calls.length).toBe(before);
  });

  it("entity suffix keeps monitors distinct on a shared DSN", () => {
    const sentry = fakeSentry();
    const hb = startHeartbeat({ surface: "kl-sync-worker", entity: "personal", sentry });
    hb.stop();
    expect(sentry.calls[0].checkIn.monitorSlug).toBe("kl-sync-worker-personal");
  });

  it("missing captureCheckIn is a soft-noop, never a throw", () => {
    const hb = startHeartbeat({ surface: "s", sentry: {} });
    expect(() => hb.stop()).not.toThrow();
  });
});

describe("beatOnce wire semantics", () => {
  it("subtracts intervalMinutes from the operator margin", async () => {
    const sentry = fakeSentry();
    await beatOnce({
      surface: "s",
      sentry,
      schedule: "0 * * * *",
      checkinMarginMinutes: 180,
      intervalMinutes: 60,
    });
    const cfg = sentry.calls[0].monitorConfig as { checkinMargin: number };
    expect(cfg.checkinMargin).toBe(120);
  });

  it("passes raw margin without intervalMinutes", async () => {
    const sentry = fakeSentry();
    await beatOnce({ surface: "s", sentry, schedule: "0 * * * *", checkinMarginMinutes: 15 });
    expect((sentry.calls[0].monitorConfig as { checkinMargin: number }).checkinMargin).toBe(15);
  });

  it("canary failure sends status=error", async () => {
    const sentry = fakeSentry();
    await beatOnce({
      surface: "s",
      sentry,
      canary: () => {
        throw new Error("stalled");
      },
    });
    expect(sentry.calls[0].checkIn.status).toBe("error");
  });

  it("manolii entity slug stays plain", async () => {
    const sentry = fakeSentry();
    await beatOnce({ surface: "finance-import", entity: "manolii", sentry });
    expect(sentry.calls[0].checkIn.monitorSlug).toBe("finance-import");
  });
});
