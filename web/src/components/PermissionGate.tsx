/**
 * Wraps a clickable element (button, link, …) and disables it when the
 * caller lacks the required RBAC capability. A tooltip explains which
 * role they currently hold vs which role would be needed.
 *
 * Usage:
 * ```tsx
 * <PermissionGate need="can_start_stop" permissions={perms}>
 *   <button onClick={start}>Start cluster</button>
 * </PermissionGate>
 * ```
 *
 * Source-of-truth contract: ``api/services/me_permissions.py``. When
 * ``permissions.degraded === true`` the gate stays OPEN so a transient
 * ARM enumeration failure does not lock the operator out. Real
 * authorization is enforced server-side at submit time.
 */

import { cloneElement, isValidElement } from "react";
import type { ReactNode } from "react";

import type { CallerPermissionsResponse } from "@/api/me";
import type { PermissionCapability } from "@/hooks/usePermissions";

const CAPABILITY_LABEL: Record<PermissionCapability, string> = {
  can_read: "read this resource",
  can_write: "modify this resource",
  can_start_stop: "start or stop this cluster",
  can_delete: "delete this cluster",
  can_submit_blast: "submit BLAST jobs",
  can_build_acr: "build ACR images",
  can_grant_rbac: "grant Azure RBAC roles",
};

const CAPABILITY_REQUIRED_ROLE: Record<PermissionCapability, string> = {
  can_read: "Reader (or Contributor / Owner)",
  can_write: "Contributor or Owner",
  can_start_stop: "Contributor or Azure Kubernetes Service Contributor",
  can_delete: "Owner or Azure Kubernetes Service RBAC Cluster Admin",
  can_submit_blast: "Contributor + Storage Blob Data Contributor",
  can_build_acr: "Contributor or Owner",
  can_grant_rbac: "Owner or User Access Administrator",
};

export function permissionDeniedTooltip(
  need: PermissionCapability,
  permissions: CallerPermissionsResponse,
): string {
  const action = CAPABILITY_LABEL[need];
  const required = CAPABILITY_REQUIRED_ROLE[need];
  const have =
    permissions.matched_role_names.length > 0
      ? permissions.matched_role_names.join(", ")
      : "no Azure RBAC role at this scope";
  return `You do not have permission to ${action}. You hold: ${have}. You need: ${required}.`;
}

export interface PermissionGateProps {
  /** Capability that must be true for the wrapped element to stay enabled. */
  need: PermissionCapability;
  /** Permissions snapshot from ``usePermissions``. */
  permissions: CallerPermissionsResponse;
  /** When true, render the wrapped element as enabled regardless of
   *  ``need``. Useful for admin-only overrides that should bypass the
   *  gate (e.g. allow Owner to always click through). Defaults to false. */
  ignore?: boolean;
  /** Wrapped element. Must accept ``disabled`` and ``title`` props. */
  children: ReactNode;
  /** When false, render the wrapped element instead of hiding it (the
   *  default). Set ``hideInsteadOfDisable={true}`` for surfaces where a
   *  disabled control would be confusing (e.g. a hidden navigation
   *  link). Defaults to false. */
  hideInsteadOfDisable?: boolean;
}

/** Hide / disable a clickable element when the caller lacks ``need``.
 *  When ``permissions.degraded`` is true the gate stays OPEN. */
export function PermissionGate({
  need,
  permissions,
  ignore = false,
  children,
  hideInsteadOfDisable = false,
}: PermissionGateProps): JSX.Element | null {
  const allowed = ignore || permissions.degraded || permissions[need];
  if (allowed) {
    return <>{children}</>;
  }
  if (hideInsteadOfDisable) {
    return null;
  }
  const tooltip = permissionDeniedTooltip(need, permissions);
  if (isValidElement(children)) {
    // Disable the wrapped element and inject a tooltip. We don't override
    // any existing onClick handler — disabled buttons in React ignore
    // clicks at the DOM level anyway.
    const childProps = (children.props ?? {}) as Record<string, unknown>;
    const existingTitle =
      typeof childProps.title === "string" ? childProps.title : "";
    const mergedTitle = existingTitle ? `${existingTitle} — ${tooltip}` : tooltip;
    return cloneElement(
      children as React.ReactElement<{
        disabled?: boolean;
        title?: string;
        "aria-disabled"?: boolean | "true" | "false";
      }>,
      {
        disabled: true,
        title: mergedTitle,
        "aria-disabled": true,
      },
    );
  }
  // Non-element children (string, fragment, …) get wrapped in a span
  // carrying the tooltip + a muted style. This is fallback only;
  // callers should always pass a real element.
  return (
    <span
      title={tooltip}
      aria-disabled="true"
      style={{ opacity: 0.5, cursor: "not-allowed" }}
    >
      {children}
    </span>
  );
}
