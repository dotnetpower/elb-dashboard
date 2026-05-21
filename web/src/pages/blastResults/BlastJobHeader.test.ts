import { describe, expect, it } from "vitest";

import { buildTimingMetrics } from "./BlastJobHeader";

describe("buildTimingMetrics", () => {
  it("separates dashboard workflow time from K8s and container runtime", () => {
    const metrics = buildTimingMetrics({
      createdAt: "2026-05-21T03:55:25+00:00",
      updatedAt: "2026-05-21T03:59:39+00:00",
      customStatus: {
        steps: {
          submitting: { duration_ms: 106_000 },
          running: {
            duration_ms: 20_000,
            k8s: {
              started_at: "2026-05-21T03:58:08Z",
              completed_at: "2026-05-21T03:58:28Z",
              blast_container_duration_ms: 7_000,
              results_export_container_duration_ms: 14_000,
            },
          },
          exporting_results: { duration_ms: 69_000 },
        },
      },
    });

    expect(metrics.map((metric) => [metric.label, metric.value])).toEqual([
      ["Workflow", "4m 14s"],
      ["Compute", "7s"],
      ["K8s runtime", "20s"],
      ["Submit path", "1m 46s"],
      ["Export containers", "14s"],
    ]);
    expect(metrics.at(-1)?.title).toContain("dashboard export/finalize path was 1m 9s");
  });

  it("falls back to K8s timestamps when step duration is absent", () => {
    const metrics = buildTimingMetrics({
      createdAt: null,
      customStatus: {
        steps: {
          running: {
            k8s: {
              started_at: "2026-05-21T03:58:08Z",
              completed_at: "2026-05-21T03:58:28Z",
            },
          },
        },
      },
    });

    expect(metrics.map((metric) => [metric.label, metric.value])).toEqual([
      ["K8s runtime", "20s"],
    ]);
  });

  it("labels export fallback as the dashboard export path", () => {
    const metrics = buildTimingMetrics({
      createdAt: null,
      customStatus: {
        steps: {
          exporting_results: { duration_ms: 69_000 },
        },
      },
    });

    expect(metrics.map((metric) => [metric.label, metric.value])).toEqual([
      ["Export path", "1m 9s"],
    ]);
  });
});