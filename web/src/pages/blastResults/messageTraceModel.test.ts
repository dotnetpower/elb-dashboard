import { describe, expect, it } from "vitest";

import type { BlastMessageTrace } from "@/api/blast.types";

import {
  CANONICAL_ORDER,
  STAGE_LABELS,
  fmtTraceMs,
  stageDisplayState,
  traceTerminallyFailed,
  visibleTraceStages,
} from "./messageTraceModel";

function trace(stages: string[]): BlastMessageTrace {
  return {
    stages: stages.map((stage, i) => ({
      stage,
      ts: `2026-06-14T00:00:0${i}+00:00`,
    })),
    metrics: { queue_dwell_ms: null, submit_latency_ms: null, e2e_ms: null },
    terminal_stage: null,
    last_stage: stages[stages.length - 1] ?? null,
  };
}

describe("fmtTraceMs", () => {
  it("renders em-dash for null/non-finite", () => {
    expect(fmtTraceMs(null)).toBe("—");
    expect(fmtTraceMs(Number.NaN)).toBe("—");
  });
  it("renders ms under a second", () => {
    expect(fmtTraceMs(0)).toBe("0 ms");
    expect(fmtTraceMs(999)).toBe("999 ms");
  });
  it("renders seconds under a minute", () => {
    expect(fmtTraceMs(2000)).toBe("2.0 s");
    expect(fmtTraceMs(30500)).toBe("30.5 s");
  });
  it("renders minutes + seconds at/over a minute", () => {
    expect(fmtTraceMs(60000)).toBe("1m 0s");
    expect(fmtTraceMs(95000)).toBe("1m 35s");
  });
});

describe("visibleTraceStages", () => {
  it("returns empty for no stages", () => {
    expect(visibleTraceStages(trace([]))).toEqual([]);
  });

  it("shows stages up to and including the last reached one, in canonical order", () => {
    // Out-of-order input; last reached is 'submitted'.
    const v = visibleTraceStages(trace(["received", "enqueued", "submitted"]));
    expect(v).toEqual(["enqueued", "received", "row_created", "routed", "submitted"]);
  });

  it("keeps unreached intermediate stages visible (as pending) but omits future ones", () => {
    const v = visibleTraceStages(trace(["enqueued", "submitted"]));
    // received/row_created/routed are intermediate (kept); running+ omitted.
    expect(v).toEqual(["enqueued", "received", "row_created", "routed", "submitted"]);
    expect(v).not.toContain("running");
  });

  it("includes the terminal + delivery stage when reached", () => {
    const v = visibleTraceStages(
      trace(["enqueued", "received", "submitted", "succeeded", "completion_published"]),
    );
    expect(v[v.length - 1]).toBe("completion_published");
    expect(v).toContain("succeeded");
  });
});

describe("stage constants", () => {
  it("has a label for every canonical stage", () => {
    for (const stage of CANONICAL_ORDER) {
      expect(STAGE_LABELS[stage]).toBeTruthy();
    }
  });
  it("canonical order has no duplicates", () => {
    expect(new Set(CANONICAL_ORDER).size).toBe(CANONICAL_ORDER.length);
  });
});

describe("stageDisplayState (terminal-failure handling)", () => {
  const succeededReached = new Set([
    "enqueued",
    "received",
    "submitted",
    "succeeded",
    "completion_published",
  ]);
  const failedReached = new Set([
    "enqueued",
    "received",
    "submitted",
    "failed",
    "completion_published",
  ]);

  it("traceTerminallyFailed detects failed / dead_letter", () => {
    expect(traceTerminallyFailed(succeededReached)).toBe(false);
    expect(traceTerminallyFailed(failedReached)).toBe(true);
    expect(traceTerminallyFailed(new Set(["dead_letter"]))).toBe(true);
  });

  it("succeeded job: reached stages are done, delivery is done", () => {
    expect(stageDisplayState("submitted", succeededReached, false)).toBe("done");
    expect(stageDisplayState("succeeded", succeededReached, false)).toBe("done");
    expect(stageDisplayState("completion_published", succeededReached, false)).toBe(
      "done",
    );
  });

  it("failed job: terminal stage is failed; result-delivered is canceled (not done)", () => {
    expect(stageDisplayState("failed", failedReached, true)).toBe("failed");
    // The completion event was published for a failure → no result delivered.
    expect(stageDisplayState("completion_published", failedReached, true)).toBe(
      "canceled",
    );
  });

  it("failed job: skipped success-path stages are canceled, not pending", () => {
    // running / succeeded never happened on a fail-at-submit job.
    expect(stageDisplayState("running", failedReached, true)).toBe("canceled");
    expect(stageDisplayState("succeeded", failedReached, true)).toBe("canceled");
  });

  it("non-failed job: unreached future stages stay pending", () => {
    const inFlight = new Set(["enqueued", "received", "submitted"]);
    expect(stageDisplayState("running", inFlight, false)).toBe("pending");
    expect(stageDisplayState("succeeded", inFlight, false)).toBe("pending");
  });
});
