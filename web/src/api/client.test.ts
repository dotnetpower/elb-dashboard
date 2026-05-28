import { describe, expect, it } from "vitest";

import { formatApiError } from "./client";

class FakeApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, body: unknown, message?: string) {
    super(message ?? `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

describe("formatApiError — openapi /v1/ready upstream codes", () => {
  it("maps openapi_not_ready + no_workload_nodes to scale-up hint", () => {
    const err = new FakeApiError(503, {
      detail: {
        code: "openapi_not_ready",
        upstream_code: "no_workload_nodes",
        message: "No Ready nodes match label 'workload=blast'",
      },
    });
    const msg = formatApiError(err, "blast");
    expect(msg).toContain("BLAST API is not ready");
    expect(msg).toContain("no_workload_nodes");
    expect(msg).toContain("Scale up the BLAST workload pool");
  });

  it("maps openapi_not_ready + openapi_pod_not_ready to pod restart hint", () => {
    const err = new FakeApiError(503, {
      detail: {
        code: "openapi_not_ready",
        upstream_code: "openapi_pod_not_ready",
        message: "elb-openapi pod is not Ready",
      },
    });
    const msg = formatApiError(err, "blast");
    expect(msg).toContain("openapi_pod_not_ready");
    expect(msg).toContain("Restart the elb-openapi pod");
  });

  it("maps openapi_unreachable to cluster-health hint", () => {
    const err = new FakeApiError(503, {
      detail: { code: "openapi_unreachable", message: "ConnectError" },
    });
    const msg = formatApiError(err, "blast");
    expect(msg).toContain("Cannot reach the BLAST API service");
  });

  it("maps 429 openapi_ready_rate_limited with limit_per_minute", () => {
    const err = new FakeApiError(429, {
      detail: { code: "openapi_ready_rate_limited", limit_per_minute: 30 },
    });
    const msg = formatApiError(err, "blast");
    expect(msg).toContain("rate-limit hit");
    expect(msg).toContain("30/min");
  });

  it("falls through to generic 503 message when no openapi code is present", () => {
    const err = new FakeApiError(503, {
      detail: { code: "some_other_code", message: "x" },
    });
    const msg = formatApiError(err, "blast");
    expect(msg).not.toContain("BLAST API is not ready");
    expect(msg).not.toContain("Cannot reach the BLAST API service");
  });

  it("handles unknown upstream_code by surfacing a generic action", () => {
    const err = new FakeApiError(503, {
      detail: {
        code: "openapi_not_ready",
        upstream_code: "totally_new_code",
        message: "future probe",
      },
    });
    const msg = formatApiError(err, "blast");
    expect(msg).toContain("totally_new_code");
    expect(msg).toContain("Check AKS cluster health");
  });
});

describe("formatApiError — 409 blocked_by_preflight envelope", () => {
  it("renders message + action per blocking gate", () => {
    const err = new FakeApiError(409, {
      detail: {
        code: "blocked_by_preflight",
        message: "elb-openapi readiness probe failed (openapi_not_ready).",
        blocking_gates: [
          {
            id: "openapi_ready",
            error_code: "openapi_not_ready",
            message: "No Ready nodes match label 'workload=blast'",
            action: "Scale up workload pool",
            action_type: "scale_up_workload_pool",
          },
        ],
        gates: [],
      },
    });
    const msg = formatApiError(err, "blast");
    expect(msg).toContain("Pre-flight check failed");
    expect(msg).toContain("No Ready nodes");
    expect(msg).toContain("Scale up workload pool");
  });

  it("joins multiple blocking gates", () => {
    const err = new FakeApiError(409, {
      detail: {
        code: "blocked_by_preflight",
        message: "two failures",
        blocking_gates: [
          {
            id: "openapi_ready",
            error_code: "openapi_unreachable",
            message: "AKS stopped",
            action: "Start cluster",
          },
          {
            id: "acr_images",
            error_code: "acr_images_missing",
            message: "ncbi/elb missing",
            action: "Build now",
          },
        ],
      },
    });
    const msg = formatApiError(err, "blast");
    expect(msg).toContain("AKS stopped");
    expect(msg).toContain("Start cluster");
    expect(msg).toContain("ncbi/elb missing");
    expect(msg).toContain("Build now");
  });

  it("falls back to generic 4xx rendering when preflight envelope is absent", () => {
    const err = new FakeApiError(409, {
      detail: { code: "some_other_conflict", message: "already running" },
    });
    const msg = formatApiError(err, "blast");
    expect(msg).not.toContain("Pre-flight check failed");
    expect(msg).toContain("already running");
  });

  it("falls back when blocking_gates is empty", () => {
    const err = new FakeApiError(409, {
      detail: { code: "blocked_by_preflight", message: "x", blocking_gates: [] },
    });
    const msg = formatApiError(err, "blast");
    expect(msg).not.toContain("Pre-flight check failed");
  });
});
