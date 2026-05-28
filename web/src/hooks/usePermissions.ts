/**
 * React Query hook for ``/api/me/permissions``.
 *
 * Returns the calling user's effective RBAC capabilities at the requested
 * scope, plus convenience flags the SPA can plug straight into
 * ``<PermissionGate>``. Cached for 60 s to match the backend's own
 * permission cache (further requests within that window are de-duped by
 * TanStack Query and never hit the network).
 *
 * Source of truth: ``api/services/me_permissions.py`` +
 * ``api/routes/me.py``. The ``degraded=true`` branch keeps every
 * ``can_*`` flag true \u2014 the SPA must NOT lock the operator out on
 * a transient ARM enumeration failure.
 */

import { useQuery } from "@tanstack/react-query";

import { meApi, type CallerPermissionsResponse } from "@/api/me";

const PERMISSIONS_STALE_MS = 60_000;

export const PERMISSIONS_QUERY_KEY = (
  subscriptionId: string,
  resourceGroup?: string,
  clusterName?: string,
) =>
  [
    "me",
    "permissions",
    subscriptionId || "",
    resourceGroup || "",
    clusterName || "",
  ] as const;

/** All capability flags returned by the backend. */
export type PermissionCapability =
  | "can_read"
  | "can_write"
  | "can_start_stop"
  | "can_delete"
  | "can_submit_blast"
  | "can_build_acr"
  | "can_grant_rbac";

/** Fail-open permissions used when the query is disabled / not yet
 *  resolved. Keeping every capability ``true`` here matches the
 *  ``degraded=true`` server-side contract (do not lock the operator
 *  out on a transient hiccup). */
const OPEN_PERMISSIONS: CallerPermissionsResponse = {
  can_read: true,
  can_write: true,
  can_start_stop: true,
  can_delete: true,
  can_submit_blast: true,
  can_build_acr: true,
  can_grant_rbac: true,
  degraded: true,
  matched_roles: [],
  matched_role_names: [],
  reason: "loading",
};

export interface UsePermissionsResult {
  permissions: CallerPermissionsResponse;
  isLoading: boolean;
  isError: boolean;
  error: unknown;
}

export function usePermissions(
  subscriptionId: string | null | undefined,
  resourceGroup?: string | null,
  clusterName?: string | null,
): UsePermissionsResult {
  const enabled = Boolean(subscriptionId);
  const query = useQuery({
    queryKey: PERMISSIONS_QUERY_KEY(
      subscriptionId || "",
      resourceGroup || undefined,
      clusterName || undefined,
    ),
    queryFn: () =>
      meApi.permissions(
        subscriptionId as string,
        resourceGroup || undefined,
        clusterName || undefined,
      ),
    staleTime: PERMISSIONS_STALE_MS,
    refetchInterval: false,
    refetchOnWindowFocus: false,
    enabled,
    // Match backend degrade-open: a network error should not lock the
    // operator out. The fallback ``OPEN_PERMISSIONS`` is used by
    // ``data ?? OPEN_PERMISSIONS`` below.
    retry: 1,
  });

  return {
    permissions: query.data ?? OPEN_PERMISSIONS,
    isLoading: query.isLoading,
    isError: query.isError,
    error: query.error,
  };
}
