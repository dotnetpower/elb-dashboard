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

import { formatLiveLogLine, stripConsoleOutputBlock } from "./StepLogSection";

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

describe("formatLiveLogLine", () => {
  it("does not label elastic-blast (terminal_exec) stderr as an error", () => {
    // ElasticBLAST routes its full INFO log to stderr by design, so the
    // line must render verbatim — no misleading `[stderr]` prefix.
    expect(
      formatLiveLogLine({
        source: "terminal_exec",
        stream: "stderr",
        line: "Splitting queries into batches",
      }),
    ).toBe("Splitting queries into batches");
  });

  it("renders terminal_exec stdout verbatim", () => {
    expect(
      formatLiveLogLine({
        source: "terminal_exec",
        stream: "stdout",
        line: "Submitting 4 jobs to cluster",
      }),
    ).toBe("Submitting 4 jobs to cluster");
  });

  it("prefixes k8s pod logs with the pod/container", () => {
    expect(
      formatLiveLogLine({
        source: "k8s",
        pod: "blast-worker-0",
        container: "blast",
        line: "running blastn",
      }),
    ).toBe("[blast-worker-0/blast] running blastn");
  });

  it("omits the container segment when absent", () => {
    expect(
      formatLiveLogLine({
        source: "k8s",
        pod: "blast-worker-0",
        line: "running blastn",
      }),
    ).toBe("[blast-worker-0] running blastn");
  });

  it("keeps a [stderr] marker for an unknown source's stderr stream", () => {
    expect(
      formatLiveLogLine({
        source: "other",
        stream: "stderr",
        line: "boom",
      }),
    ).toBe("[stderr] boom");
  });
});
