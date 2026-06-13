import { describe, expect, it } from "vitest";

import { isGrantLbSubnetRbacRecovery } from "./GrantLbSubnetRbacButton";

describe("isGrantLbSubnetRbacRecovery", () => {
  it("returns false for non-objects", () => {
    expect(isGrantLbSubnetRbacRecovery(null)).toBe(false);
    expect(isGrantLbSubnetRbacRecovery(undefined)).toBe(false);
    expect(isGrantLbSubnetRbacRecovery("oops")).toBe(false);
    expect(isGrantLbSubnetRbacRecovery(42)).toBe(false);
  });

  it("returns true when the canonical recovery_action is set", () => {
    expect(
      isGrantLbSubnetRbacRecovery({
        degraded: true,
        degraded_reason: "openapi_service_not_reachable",
        recovery_action: "grant_lb_subnet_rbac",
      }),
    ).toBe(true);
  });

  it("does NOT match the peering recovery action", () => {
    expect(
      isGrantLbSubnetRbacRecovery({ recovery_action: "peer_with_platform" }),
    ).toBe(false);
  });

  it("unwraps a nested `body` payload (thrown ApiError shape)", () => {
    expect(
      isGrantLbSubnetRbacRecovery({
        status: 503,
        body: { recovery_action: "grant_lb_subnet_rbac" },
      }),
    ).toBe(true);
  });

  it("returns false when there is no recovery action", () => {
    expect(isGrantLbSubnetRbacRecovery({ degraded_reason: "cluster_stopped" })).toBe(
      false,
    );
  });
});
