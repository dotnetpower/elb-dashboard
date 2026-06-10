/**
 * Bootstrap gate for the optional dashboard RBAC entry check.
 *
 * The SPA's identity bootstrap (`GET /api/me`) carries an opt-in backend gate
 * (`require_dashboard_access`). When the operator enables
 * `ENFORCE_DASHBOARD_RBAC=true`, a tenant member with no Azure read role on the
 * dashboard scope receives a 403 with body `{ code: "dashboard_access_denied",
 * message, resource_group }`. This hook resolves that bootstrap call once and
 * maps it to a tri-state the top-level app gate can branch on.
 *
 * Fail-open by design: only an explicit `dashboard_access_denied` 403 denies
 * entry. A transient network error, a different 4xx/5xx, or a successful 200
 * all resolve to `granted` so a backend hiccup never locks out a legitimate
 * operator (mirrors the backend's degrade-open contract — ARM still enforces
 * real authorization on every data-plane action).
 *
 * Source of truth for the contract: `api/services/dashboard_access.py`.
 */

import { useQuery } from "@tanstack/react-query";

import { meApi } from "@/api/me";
import type { ApiError } from "@/api/client";

export const DASHBOARD_ACCESS_DENIED_CODE = "dashboard_access_denied";

export type DashboardAccessStatus = "loading" | "granted" | "denied";

export interface DashboardAccessResult {
  status: DashboardAccessStatus;
  /** Human-readable reason, present only when `status === "denied"`. */
  message: string;
}

/** Extract the denial message when `err` is the entry-gate 403, else null.
 *
 *  The backend's global HTTPException handler flattens a dict `detail` to the
 *  top level of the JSON body, so the discriminator lives at `body.code`
 *  (NOT `body.detail.code`). */
function denialMessage(err: unknown): string | null {
  if (!(err instanceof Error)) return null;
  const apiErr = err as Partial<ApiError>;
  if (apiErr.status !== 403) return null;
  const body = apiErr.body;
  if (!body || typeof body !== "object") return null;
  const code = (body as { code?: unknown }).code;
  if (code !== DASHBOARD_ACCESS_DENIED_CODE) return null;
  const message = (body as { message?: unknown }).message;
  return typeof message === "string" && message.trim()
    ? message.trim()
    : "You do not have permission to access this dashboard.";
}

export function useDashboardAccessGate(): DashboardAccessResult {
  const query = useQuery({
    queryKey: ["me", "access-gate"],
    queryFn: () => meApi.get(),
    // A 403 entry-gate decision is deterministic; do not retry. A transient
    // error resolves to `granted` below (fail-open), so retrying only delays
    // the dashboard for everyone when the gate is OFF (the common case).
    retry: false,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });

  if (query.isLoading) {
    return { status: "loading", message: "" };
  }
  if (query.isError) {
    const message = denialMessage(query.error);
    if (message) {
      return { status: "denied", message };
    }
    // Any non-gate error → fail open so a backend hiccup never locks out.
    return { status: "granted", message: "" };
  }
  return { status: "granted", message: "" };
}
