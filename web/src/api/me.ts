/**
 * Typed client for `/api/me`.
 *
 * The backend response is the validated caller's identity claims plus the
 * list of Azure subscriptions visible to the api sidecar's managed identity
 * (or, in local dev, the developer's `az login` session). The SPA uses the
 * visible list to detect stale workspace settings (a `subscriptionId` saved
 * in `localStorage` that the current credential cannot see) and to render a
 * targeted diagnostics banner.
 *
 * Source of truth: `api/routes/me.py`. Renaming any of these fields requires
 * a coordinated backend change.
 */

import { api } from "@/api/client";

export interface CallerSubscription {
  subscriptionId: string;
  displayName: string;
  tenantId: string;
  state: string;
}

export interface CallerIdentityResponse {
  object_id: string | null;
  tenant_id: string | null;
  upn: string | null;
  subscriptions: CallerSubscription[];
  /** Set only when the backend could not enumerate subscriptions. */
  subscriptions_error?: string;
}

/** Effective RBAC capabilities for the calling user at a scope.
 *
 *  Returned by ``GET /api/me/permissions?subscription_id=…``. The SPA uses
 *  this to disable Start/Stop/Delete/Submit/Build buttons (with a tooltip
 *  explaining the missing role) when the signed-in user lacks the
 *  underlying Azure RBAC role at the requested scope.
 *
 *  ``degraded=true`` means the backend could not enumerate the caller's
 *  role assignments \u2014 the SPA must treat this as "do not disable"
 *  (every ``can_*`` is set to ``true``) so a transient ARM hiccup never
 *  locks the operator out. ARM still enforces real authorization at
 *  submit time.
 */
export interface CallerPermissionsResponse {
  can_read: boolean;
  can_write: boolean;
  can_start_stop: boolean;
  can_delete: boolean;
  can_submit_blast: boolean;
  can_build_acr: boolean;
  can_grant_rbac: boolean;
  degraded: boolean;
  matched_roles: string[];
  matched_role_names: string[];
  reason: string;
}

/** One effective role assignment as seen from a target resource group.
 *
 *  Returned inside ``AccessReviewResponse``. Mirrors the Azure portal
 *  "View my access" rows: the role display name, where the assignment
 *  actually lives (``scope_level``), and whether the resource group
 *  inherits it from a broader scope.
 */
export interface AccessReviewRow {
  role_name: string;
  role_guid: string;
  scope_level: "subscription" | "management_group" | "resource_group" | "resource" | "other";
  inherited: boolean;
  assignment_scope: string;
}

/** Effective access for the calling user at one resource group. */
export interface AccessReviewGroup {
  resource_group: string;
  scope: string;
  assignments: AccessReviewRow[];
  /** True when enumeration failed — the SPA must NOT read this as
   *  "has access". It usually means the caller lacks
   *  ``Microsoft.Authorization/roleAssignments/read``, which is itself a
   *  finding worth surfacing during tenant onboarding. */
  degraded: boolean;
  reason: string;
}

/** Whose access an access review describes. */
export interface AccessReviewPrincipal {
  /** ``"user"`` (signed-in caller) or ``"dashboard_identity"`` (the shared
   *  managed identity the Container App runs as). */
  kind: "user" | "dashboard_identity";
  object_id: string;
  /** False when the principal could not be resolved — e.g. the dashboard
   *  managed identity's principal id is not exported in local dev. */
  available: boolean;
}

/** Per-resource-group access review for the calling user.
 *
 *  Returned by ``GET /api/me/access-review?subscription_id=…&resource_group=…``
 *  (repeat ``resource_group`` to review several at once; ``target=me|dashboard``
 *  selects the signed-in caller or the dashboard managed identity). Reproduces
 *  the portal "View my access" experience so an operator can diagnose why an
 *  action fails in a freshly-onboarded tenant. Unlike ``permissions`` this
 *  does NOT degrade open. */
export interface AccessReviewResponse {
  subscription_id: string;
  principal: AccessReviewPrincipal;
  groups: AccessReviewGroup[];
}

export type AccessReviewTarget = "me" | "dashboard";

export const meApi = {
  get: () => api.get<CallerIdentityResponse>("/me"),
  permissions: (
    subscriptionId: string,
    resourceGroup?: string,
    clusterName?: string,
  ) => {
    const qs = new URLSearchParams();
    qs.set("subscription_id", subscriptionId);
    if (resourceGroup) qs.set("resource_group", resourceGroup);
    if (clusterName) qs.set("cluster_name", clusterName);
    return api.get<CallerPermissionsResponse>(
      `/me/permissions?${qs.toString()}`,
    );
  },
  accessReview: (
    subscriptionId: string,
    resourceGroups: string[],
    target: AccessReviewTarget = "me",
  ) => {
    const qs = new URLSearchParams();
    qs.set("subscription_id", subscriptionId);
    for (const rg of resourceGroups) {
      if (rg) qs.append("resource_group", rg);
    }
    if (target !== "me") qs.set("target", target);
    return api.get<AccessReviewResponse>(
      `/me/access-review?${qs.toString()}`,
    );
  },
};

