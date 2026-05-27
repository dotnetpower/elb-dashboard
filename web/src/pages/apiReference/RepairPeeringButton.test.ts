import { describe, expect, it } from "vitest";

import { isPeerWithPlatformRecovery } from "./RepairPeeringButton";

describe("isPeerWithPlatformRecovery", () => {
  it("returns false for null / undefined / non-object payloads", () => {
    expect(isPeerWithPlatformRecovery(null)).toBe(false);
    expect(isPeerWithPlatformRecovery(undefined)).toBe(false);
    expect(isPeerWithPlatformRecovery("oops")).toBe(false);
    expect(isPeerWithPlatformRecovery(42)).toBe(false);
  });

  it("returns true when the canonical recovery_action field is set", () => {
    expect(
      isPeerWithPlatformRecovery({
        code: "openapi_upstream_unreachable",
        recovery_action: "peer_with_platform",
        recovery_hint: "...",
      }),
    ).toBe(true);
  });

  it("returns true for legacy `code` strings without recovery_action", () => {
    expect(
      isPeerWithPlatformRecovery({ code: "openapi_upstream_unreachable" }),
    ).toBe(true);
    expect(
      isPeerWithPlatformRecovery({ code: "openapi_service_not_reachable" }),
    ).toBe(true);
  });

  it("returns true for spec-degraded payloads via degraded_reason", () => {
    expect(
      isPeerWithPlatformRecovery({
        degraded: true,
        degraded_reason: "openapi_endpoint_unreachable",
      }),
    ).toBe(true);
    expect(
      isPeerWithPlatformRecovery({
        degraded: true,
        degraded_reason: "openapi_service_not_reachable",
      }),
    ).toBe(true);
  });

  it("walks into ApiError.body so callers can pass the thrown error directly", () => {
    const apiError = {
      name: "Error",
      message: "HTTP 502",
      status: 502,
      body: {
        code: "openapi_upstream_unreachable",
        recovery_action: "peer_with_platform",
      },
    };
    expect(isPeerWithPlatformRecovery(apiError)).toBe(true);
  });

  it("rejects unrelated error codes so the affordance does not over-trigger", () => {
    expect(
      isPeerWithPlatformRecovery({
        code: "openapi_unsafe_transport",
        message: "Refusing to send admin token to non-private IP",
      }),
    ).toBe(false);
    expect(isPeerWithPlatformRecovery({ degraded_reason: "cluster_stopped" })).toBe(
      false,
    );
  });
});
