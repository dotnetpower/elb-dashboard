import { describe, expect, it } from "vitest";

import type { BlastJobSummary, BlastJobTiming } from "@/api/endpoints";

import { recentTimingStats } from "./timingStats";

function timing(
  timeToResult: number,
  queue: number,
  processing: number,
  queueComplete = true,
): BlastJobTiming {
  return {
    schema_version: 1,
    result_ready_at: null,
    completion_published_at: null,
    time_to_result_seconds: timeToResult,
    total_queue_seconds: queue,
    service_bus_queue_seconds: null,
    submit_seconds: null,
    execution_queue_seconds: queue,
    processing_seconds: processing,
    execution_elapsed_seconds: queue + processing,
    status_delivery_seconds: null,
    unattributed_seconds: 0,
    queue_complete: queueComplete,
    breakdown_complete: queueComplete,
    phases: {
      orchestration_seconds: null,
      k8s_setup_seconds: null,
      blast_seconds: null,
      export_seconds: null,
      finalizer_seconds: null,
    },
  };
}

const job = (id: string, value?: BlastJobTiming) =>
  ({ job_id: id, timing: value }) as BlastJobSummary;

describe("recentTimingStats", () => {
  it("computes nearest-rank p50/p95 from complete samples", () => {
    const stats = recentTimingStats([
      job("a", timing(100, 20, 80)),
      job("b", timing(200, 100, 100)),
      job("c", timing(300, 180, 120)),
      job("d", timing(400, 260, 140)),
      job("e", timing(500, 340, 160)),
    ]);

    expect(stats.timeToResultP50).toBe(300);
    expect(stats.timeToResultP95).toBe(500);
    expect(stats.queueP50).toBe(180);
    expect(stats.queueP95).toBe(340);
    expect(stats.processingP50).toBe(120);
    expect(stats.processingP95).toBe(160);
    expect(stats.pressureSamples).toBe(5);
    expect(stats.queuePressure).toBe("high");
  });

  it("excludes incomplete queue samples without dropping processing", () => {
    const stats = recentTimingStats([
      job("complete", timing(100, 20, 80)),
      job("legacy", timing(200, 100, 100, false)),
      job("missing"),
    ]);

    expect(stats.completeTimeToResultSamples).toBe(1);
    expect(stats.completeQueueSamples).toBe(1);
    expect(stats.processingSamples).toBe(2);
    expect(stats.pressureSamples).toBe(1);
    expect(stats.totalJobs).toBe(3);
    expect(stats.queuePressure).toBe("unknown");
  });

  it("reports unknown pressure without enough evidence", () => {
    const stats = recentTimingStats([job("missing")]);
    expect(stats.queuePressure).toBe("unknown");
    expect(stats.queueP95).toBeNull();
  });
});
