import { describe, expect, it } from "vitest";

import { buildTimingMetrics, resolveQueryHeaderId } from "./BlastJobHeader";

describe("buildTimingMetrics", () => {
  it("renders immutable time-to-result components instead of mutable workflow time", () => {
    const metrics = buildTimingMetrics({
      createdAt: "2026-09-11T02:14:36+00:00",
      updatedAt: "2026-09-11T03:30:31+00:00",
      timing: {
        schema_version: 1,
        result_ready_at: "2026-09-11T02:18:34Z",
        completion_published_at: "2026-09-11T02:19:15Z",
        time_to_result_seconds: 299,
        total_queue_seconds: 148,
        service_bus_queue_seconds: 62,
        submit_seconds: 13,
        execution_queue_seconds: 86,
        processing_seconds: 138,
        execution_elapsed_seconds: 224,
        status_delivery_seconds: 41,
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

    expect(metrics.map((metric) => [metric.label, metric.value])).toEqual([
      ["Time to result", "4m 59s"],
      ["Queue wait", "2m 28s"],
      ["Submit", "13s"],
      ["Processing", "2m 18s"],
      ["Status delivery", "41s"],
    ]);
    expect(metrics.some((metric) => metric.label === "Workflow")).toBe(false);
  });

  it("does not promote mutable workflow time into terminal timing", () => {
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

describe("resolveQueryHeaderId", () => {
  it("prefers the dashboard payload query identity", () => {
    expect(
      resolveQueryHeaderId({ query_id: "NC_003310.1", query_file: "q.fa" }, "ignored"),
    ).toBe("NC_003310.1");
  });

  it("reads the first query_metadata record id", () => {
    expect(
      resolveQueryHeaderId(
        { query_metadata: { records: [{ query_id: "seq-7", length: 120 }] } },
        null,
      ),
    ).toBe("seq-7");
  });

  it("basenames a dashboard query_file path", () => {
    expect(
      resolveQueryHeaderId({ query_file: "queries/uploads/probe/BRCA1.fa" }, null),
    ).toBe("BRCA1.fa");
  });

  it("digs the external payload for OpenAPI-submitted jobs", () => {
    // External jobs nest the upstream job under payload.external and never set
    // the dashboard payload query fields.
    expect(
      resolveQueryHeaderId(
        { external: { query_file: "queries/abc123.fa" } },
        "abc123.fa",
      ),
    ).toBe("abc123.fa");
  });

  it("falls back to the external `query` field", () => {
    expect(
      resolveQueryHeaderId({ external: { query: "az://stg/queries/run.fa" } }, null),
    ).toBe("run.fa");
  });

  it("falls back to the top-level query_label when nothing else is present", () => {
    // Direct /v1/jobs submits store no query identity upstream, so the backend
    // projection resolves query_label to the generic placeholder. Showing it
    // is still better than a bare em-dash.
    expect(resolveQueryHeaderId({ external: {} }, "query.fa")).toBe("query.fa");
  });

  it("returns null when there is no query identity at all", () => {
    expect(resolveQueryHeaderId(undefined, null)).toBeNull();
    expect(resolveQueryHeaderId({ external: {} }, "  ")).toBeNull();
  });
});
