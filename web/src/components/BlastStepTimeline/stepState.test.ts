import { describe, expect, it } from "vitest";

import { getTimelineStepState, resolveActiveStepIndex } from "./stepState";

const STAGING_DB_INDEX = 3;
const SUBMITTING_INDEX = 4;

describe("BLAST step state", () => {
  it("advances the active pointer past skipped staging", () => {
    const stepsData = {
      staging_db: { status: "skipped", skipped: true },
    };

    expect(resolveActiveStepIndex("staging_db", stepsData)).toBe(SUBMITTING_INDEX);
    expect(
      getTimelineStepState({
        phase: "staging_db",
        idx: STAGING_DB_INDEX,
        key: "staging_db",
        stepsData,
        failedStepIdx: -1,
      }),
    ).toBe("skipped");
    expect(
      getTimelineStepState({
        phase: "staging_db",
        idx: SUBMITTING_INDEX,
        key: "submitting",
        stepsData,
        failedStepIdx: -1,
      }),
    ).toBe("active");
  });

  it("uses server running status for the warmup timer", () => {
    const stepsData = {
      warming_up: { status: "running", started_at: "2026-05-21T06:01:26+00:00" },
    };

    expect(
      getTimelineStepState({
        phase: "warming_up",
        idx: 1,
        key: "warming_up",
        stepsData,
        failedStepIdx: -1,
      }),
    ).toBe("active");
  });

  it("treats warmup_ready as a completed warmup checkpoint", () => {
    const stepsData = {
      warming_up: { status: "completed", success: true },
    };

    expect(
      getTimelineStepState({
        phase: "warmup_ready",
        idx: 1,
        key: "warming_up",
        stepsData,
        failedStepIdx: -1,
      }),
    ).toBe("done");
  });
});