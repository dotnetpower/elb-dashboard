import { describe, expect, it } from "vitest";

import type { BlastJobSummary } from "@/api/endpoints";

import {
  formatTimingSeconds,
  stableProcessingSeconds,
  stableQueueSeconds,
  stableTimeToResultSeconds,
} from "./timingModel";

const job = (partial: Partial<BlastJobSummary>) => partial as BlastJobSummary;

describe("stable BLAST timing", () => {
  it("prefers the server timing contract over legacy fields", () => {
    const value = job({
      run_seconds: 999,
      queue_wait_seconds: 888,
      elapsed_seconds: 777,
      timing: {
        schema_version: 1,
        result_ready_at: null,
        completion_published_at: null,
        time_to_result_seconds: 299,
        total_queue_seconds: 148,
        service_bus_queue_seconds: 62,
        submit_seconds: 13,
        execution_queue_seconds: 86,
        processing_seconds: 138,
        execution_elapsed_seconds: 224,
        status_delivery_seconds: 42,
        unattributed_seconds: 0,
        queue_complete: true,
        breakdown_complete: true,
        phases: {
          orchestration_seconds: 75,
          k8s_setup_seconds: 60,
          blast_seconds: 22,
          export_seconds: 8,
          finalizer_seconds: 32,
        },
      },
    });

    expect(stableProcessingSeconds(value)).toBe(138);
    expect(stableQueueSeconds(value)).toBe(148);
    expect(stableTimeToResultSeconds(value)).toBe(299);
  });

  it("falls back to legacy immutable seconds", () => {
    const value = job({ run_seconds: 20, queue_wait_seconds: 5, elapsed_seconds: 25 });
    expect(stableProcessingSeconds(value)).toBe(20);
    expect(stableQueueSeconds(value)).toBe(5);
    expect(stableTimeToResultSeconds(value)).toBe(25);
  });

  it("formats compact timing without fabricating invalid values", () => {
    expect(formatTimingSeconds(299)).toBe("4m 59s");
    expect(formatTimingSeconds(138)).toBe("2m 18s");
    expect(formatTimingSeconds(-1)).toBe("—");
    expect(formatTimingSeconds(null)).toBe("—");
  });
});
