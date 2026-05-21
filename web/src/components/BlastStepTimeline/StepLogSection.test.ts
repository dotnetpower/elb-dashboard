/**
 * Tests for BlastStepTimeline/StepLogSection.tsx helpers.
 *
 * Responsibility: Lock the de-duplication contract between the JobState
 * snapshot's live-console block and the live SSE stream so the dashboard
 * never renders the same lines twice.
 * Edit boundaries: Test only `stripConsoleOutputBlock` here.
 * Key entry points: `test stripConsoleOutputBlock`.
 * Risky contracts: Header strings must match buildStepLog.ts.
 * Validation: `cd web && npm test -- StepLogSection`.
 */
import { describe, expect, it } from "vitest";

import { stripConsoleOutputBlock } from "./StepLogSection";

describe("stripConsoleOutputBlock", () => {
  it("removes the snapshot live console block while keeping the prologue", () => {
    const log =
      "Running elastic-blast submit...\n\n  helper job : job-abc\n\n--- Live Console Output ---\n[parallel-prep] running 4 azcopy checks\nUpload workfiles";
    expect(stripConsoleOutputBlock(log)).toBe(
      "Running elastic-blast submit...\n\n  helper job : job-abc",
    );
  });

  it("removes the `--- Console Output ---` block on done state", () => {
    const log =
      "✓ Submitted successfully.\n\n--- Console Output ---\nfoo\nbar";
    expect(stripConsoleOutputBlock(log)).toBe("✓ Submitted successfully.");
  });

  it("returns the log untouched when no console block is present", () => {
    const log = "Starting elastic-blast submit helper job...";
    expect(stripConsoleOutputBlock(log)).toBe(log);
  });

  it("returns empty string for empty input", () => {
    expect(stripConsoleOutputBlock("")).toBe("");
  });
});
