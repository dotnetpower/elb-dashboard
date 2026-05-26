import { describe, expect, it } from "vitest";

import { getTimelineStepState, resolveActiveStepIndex } from "./stepState";

const STAGING_DB_INDEX = 3;
const SUBMITTING_INDEX = 4;

describe("BLAST step state", () => {
  it("maps missing database failures to Prepare Run", () => {
    expect(resolveActiveStepIndex("database_unavailable", {})).toBe(0);
    expect(
      getTimelineStepState({
        phase: "database_unavailable",
        idx: 0,
        key: "preparing",
        stepsData: { preparing: { status: "failed" } },
        failedStepIdx: 0,
      }),
    ).toBe("error");
  });

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

  it("never renders a spinner when the parent job is in a failure phase", () => {
    // Guards against the orphan-`running` step bug: a Celery worker crash
    // leaves `submitting.status="running"` while the top-level row is later
    // reconciled to `failed`. The timeline must show error/skipped, never
    // active (which would spin forever).
    const stepsData = {
      preparing: { status: "completed", success: true },
      warming_up: { status: "completed", success: true },
      configuring: { status: "completed", success: true },
      staging_db: { status: "skipped", skipped: true },
      submitting: {
        status: "running",
        last_output: "Upload workfiles",
        submit_progress: { index: 4, label: "Uploading workfiles", total: 5 },
      },
    };

    expect(
      getTimelineStepState({
        phase: "failed",
        idx: SUBMITTING_INDEX,
        key: "submitting",
        stepsData,
        failedStepIdx: SUBMITTING_INDEX,
      }),
    ).toBe("error");

    // Even without a resolved failedStepIdx, the running step must not
    // render as active under a failure phase.
    expect(
      getTimelineStepState({
        phase: "failed",
        idx: SUBMITTING_INDEX,
        key: "submitting",
        stepsData,
        failedStepIdx: -1,
      }),
    ).not.toBe("active");
  });

  it("activates BLAST Run while phase is the transit `submitted` (post-submit, pre-pod)", () => {
    // `submitted` is the gap between the submit task completing and the
    // first `poll_running_status` tick reporting pods=Running. Without
    // PHASE_TO_STEP["submitted"], every step would resolve to "pending"
    // and the timeline would sit silent for 10-30 s.
    const RUNNING_INDEX = 5;
    const stepsData = {
      preparing: { status: "completed", success: true },
      warming_up: { status: "completed", success: true },
      configuring: { status: "completed", success: true },
      staging_db: { status: "skipped", skipped: true },
      submitting: { status: "completed", success: true },
    };

    expect(resolveActiveStepIndex("submitted", stepsData)).toBe(RUNNING_INDEX);
    expect(
      getTimelineStepState({
        phase: "submitted",
        idx: RUNNING_INDEX,
        key: "running",
        stepsData,
        failedStepIdx: -1,
      }),
    ).toBe("active");
  });

  it("keeps the Submit step active while waiting for a submit slot", () => {
    const stepsData = {
      preparing: { status: "completed", success: true },
      warming_up: { status: "completed", success: true },
      configuring: { status: "completed", success: true },
      staging_db: { status: "skipped", skipped: true },
    };

    expect(resolveActiveStepIndex("waiting_for_submit_slot", stepsData)).toBe(
      SUBMITTING_INDEX,
    );
    expect(
      getTimelineStepState({
        phase: "waiting_for_submit_slot",
        idx: SUBMITTING_INDEX,
        key: "submitting",
        stepsData,
        failedStepIdx: -1,
      }),
    ).toBe("active");
  });
});