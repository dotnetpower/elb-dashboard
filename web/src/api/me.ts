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

export const meApi = {
  get: () => api.get<CallerIdentityResponse>("/me"),
};
