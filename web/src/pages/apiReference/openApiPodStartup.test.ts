import { describe, expect, it } from "vitest";

import { readOpenApiPodStartup } from "./openApiPodStartup";

describe("readOpenApiPodStartup", () => {
  it("returns null for non-objects", () => {
    expect(readOpenApiPodStartup(null)).toBeNull();
    expect(readOpenApiPodStartup(undefined)).toBeNull();
    expect(readOpenApiPodStartup("oops")).toBeNull();
    expect(readOpenApiPodStartup(42)).toBeNull();
  });

  it("returns null for a healthy spec payload", () => {
    expect(readOpenApiPodStartup({ openapi: "3.1.0", paths: {} })).toBeNull();
  });

  it("returns null for the peering-unreachable degraded payload", () => {
    expect(
      readOpenApiPodStartup({
        degraded: true,
        degraded_reason: "openapi_endpoint_unreachable",
        recovery_action: "peer_with_platform",
      }),
    ).toBeNull();
  });

  it("matches the still-starting payload", () => {
    const rec = readOpenApiPodStartup({
      degraded: true,
      degraded_reason: "openapi_pod_starting",
      pod_state: "starting",
      pod_reason: "ContainerCreating",
      pod_message: "The elb-openapi pod is starting (ContainerCreating).",
    });
    expect(rec).not.toBeNull();
    expect(rec?.pod_reason).toBe("ContainerCreating");
  });

  it("matches the not-ready (crash-loop) payload", () => {
    const rec = readOpenApiPodStartup({
      degraded: true,
      degraded_reason: "openapi_pod_not_ready",
      pod_state: "failed",
      pod_reason: "CrashLoopBackOff",
    });
    expect(rec).not.toBeNull();
    expect(rec?.degraded_reason).toBe("openapi_pod_not_ready");
  });
});
