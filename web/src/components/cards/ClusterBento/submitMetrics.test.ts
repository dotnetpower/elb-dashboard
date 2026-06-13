import { describe, expect, it, vi, afterEach } from "vitest";

import { submitTimeline, submitWindow } from "./submitMetrics";
import type { BlastJobSummary } from "@/api/endpoints";

const NOW = Date.parse("2026-06-13T12:00:00Z");

function job(partial: Partial<BlastJobSummary>): BlastJobSummary {
  return partial as BlastJobSummary;
}

/** Build an ISO timestamp `minutesAgo` minutes before the frozen NOW. */
function ago(minutesAgo: number): string {
  return new Date(NOW - minutesAgo * 60_000).toISOString();
}

afterEach(() => {
  vi.useRealTimers();
});

describe("submitWindow", () => {
  it("returns null delta / avgRuntime for an empty job list", () => {
    const w = submitWindow([]);
    expect(w).toEqual({
      last15m: 0,
      last1h: 0,
      last24h: 0,
      last24hActive: 0,
      delta: null,
      avgRuntimeSec: null,
    });
  });

  it("buckets submits into the 15m / 1h / 24h windows", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const jobs = [
      job({ created_at: ago(5), status: "running" }), // in 15m, 1h, 24h
      job({ created_at: ago(40), status: "completed", updated_at: ago(30) }), // 1h, 24h
      job({ created_at: ago(120), status: "completed", updated_at: ago(118) }), // 24h
      job({ created_at: ago(60 * 30) }), // older than 24h → excluded
    ];
    const w = submitWindow(jobs);
    expect(w.last15m).toBe(1);
    expect(w.last1h).toBe(2);
    expect(w.last24h).toBe(3);
  });

  it("counts only non-terminal jobs as active and averages completed runtimes", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const jobs = [
      job({ created_at: ago(10), status: "running" }), // active
      job({ created_at: ago(20), status: "completed", updated_at: ago(18) }), // 120s runtime
      job({ created_at: ago(30), status: "failed", updated_at: ago(26) }), // 240s runtime
    ];
    const w = submitWindow(jobs);
    expect(w.last24hActive).toBe(1);
    // (120 + 240) / 2 = 180
    expect(w.avgRuntimeSec).toBe(180);
  });

  it("ignores jobs with an unparseable created_at", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const w = submitWindow([job({ created_at: "not-a-date", status: "running" })]);
    expect(w.last24h).toBe(0);
  });
});

describe("submitTimeline", () => {
  it("returns a zero-filled bucket array of the requested width", () => {
    expect(submitTimeline([], 5)).toEqual([0, 0, 0, 0, 0]);
  });

  it("places a submit into the correct per-minute bucket", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    // window = 10 min; a submit 2 min ago lands near the end of the window.
    const buckets = submitTimeline([job({ created_at: ago(2) })], 10);
    expect(buckets).toHaveLength(10);
    expect(buckets.reduce((a, b) => a + b, 0)).toBe(1);
    // 2 min ago in a 10-min window → 8 min from window start → bucket index 8.
    expect(buckets[8]).toBe(1);
  });

  it("drops submits outside the window", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const buckets = submitTimeline([job({ created_at: ago(120) })], 10);
    expect(buckets.reduce((a, b) => a + b, 0)).toBe(0);
  });
});
