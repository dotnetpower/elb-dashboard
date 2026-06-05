/**
 * Tests for BlastStepTimeline/predicates.ts.
 *
 * Responsibility: Lock in the candidate ordering of `getFailureText` so the
 * dashboard failure banner shows the authoritative orchestrator error rather
 * than a benign helper log line captured in `step.last_output`.
 * Edit boundaries: Test only `getFailureText` here; other predicates have
 * their own coverage.
 * Key entry points: `test getFailureText prefers job.error`.
 * Risky contracts: Ordering tied to BlastJobBanners.tsx rendering.
 * Validation: `cd web && npm test -- predicates.test`.
 */
import { describe, expect, it } from "vitest";

import { getFailureText, inferFailedStepKey } from "./predicates";

describe("getFailureText", () => {
  it("prefers job.error over step.last_output", () => {
    const step = {
      last_output: "[parallel-prep] running 4 azcopy checks concurrently",
    };
    const job = {
      error: "module 'api.tasks.blast' has no attribute '_tail_text'",
    };
    expect(getFailureText(step, null, null, job)).toBe(
      "module 'api.tasks.blast' has no attribute '_tail_text'",
    );
  });

  it("falls through to output.error when job.error is absent", () => {
    const step = { last_output: "tail" };
    const output = { error: "submit failed: quota exceeded" };
    expect(getFailureText(step, output, null, {})).toBe(
      "submit failed: quota exceeded",
    );
  });

  it("uses step.last_output only when no authoritative error is available", () => {
    const step = { last_output: "Upload workfiles" };
    expect(getFailureText(step, null, null, {})).toBe("Upload workfiles");
  });

  it("returns a placeholder when no candidate has content", () => {
    expect(getFailureText(undefined, null, null, {})).toBe(
      "No detailed error was recorded by the orchestrator.",
    );
  });
});

describe("inferFailedStepKey", () => {
  it("maps a K8s-stage failure to the BLAST Run step, not Submit Job", () => {
    // Regression: a search that failed at the K8s running stage (after submit
    // succeeded) used to default to "submitting" and render "Submit Job".
    const steps = {
      submitting: { phase: "submitting", status: "completed", success: true },
      running: {
        phase: "running",
        status: "failed",
        success: false,
        error: "BLAST search exited with code 2",
      },
    };
    expect(inferFailedStepKey("failed", steps, null, null)).toBe("running");
  });

  it("honors an explicit failed_step hint from the backend", () => {
    expect(
      inferFailedStepKey("failed", {}, { failed_step: "running" }, null),
    ).toBe("running");
  });

  it("falls back to submitting only when no step ran", () => {
    expect(inferFailedStepKey("failed", {}, null, null)).toBe("submitting");
  });
});
