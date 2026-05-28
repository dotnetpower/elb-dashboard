import { describe, expect, it } from "vitest";

import type { CallerPermissionsResponse } from "@/api/me";

import { permissionDeniedTooltip } from "./PermissionGate";

function _perms(overrides: Partial<CallerPermissionsResponse>): CallerPermissionsResponse {
  return {
    can_read: false,
    can_write: false,
    can_start_stop: false,
    can_delete: false,
    can_submit_blast: false,
    can_build_acr: false,
    can_grant_rbac: false,
    degraded: false,
    matched_roles: [],
    matched_role_names: [],
    reason: "",
    ...overrides,
  };
}

describe("permissionDeniedTooltip", () => {
  it("names the action the user cannot perform", () => {
    const msg = permissionDeniedTooltip("can_delete", _perms({}));
    expect(msg).toContain("delete this cluster");
  });

  it("includes the role the user currently holds when present", () => {
    const msg = permissionDeniedTooltip(
      "can_write",
      _perms({ matched_role_names: ["Reader"] }),
    );
    expect(msg).toContain("Reader");
  });

  it("falls back to 'no Azure RBAC role at this scope' when the user has none", () => {
    const msg = permissionDeniedTooltip("can_start_stop", _perms({}));
    expect(msg).toContain("no Azure RBAC role at this scope");
  });

  it("documents the role the user would need", () => {
    const msg = permissionDeniedTooltip("can_grant_rbac", _perms({}));
    expect(msg).toContain("Owner");
    expect(msg).toContain("User Access Administrator");
  });

  it("covers every PermissionCapability with a human-friendly action label", () => {
    // Critique #6: pin the capability table so adding a new can_* on
    // the backend without a matching label here is caught in CI.
    const allCapabilities = [
      "can_read",
      "can_write",
      "can_start_stop",
      "can_delete",
      "can_submit_blast",
      "can_build_acr",
      "can_grant_rbac",
    ] as const;
    for (const cap of allCapabilities) {
      const msg = permissionDeniedTooltip(cap, _perms({}));
      expect(msg.length).toBeGreaterThan(20);
      expect(msg).not.toContain("undefined");
      expect(msg).not.toContain("[object Object]");
    }
  });
});
