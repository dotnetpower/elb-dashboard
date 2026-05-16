import { api } from "@/api/client";

/**
 * Local-debug storage helpers — only meaningful when the api process is NOT
 * running inside a Container App. The backend is the source of truth for
 * "are we local?"; the SPA never decides this on its own.
 *
 * See `api/services/storage_public_access.py` and project policy §9.
 */

export interface StorageLocalDebugStatus {
  /** True only when the api sidecar is running on a developer laptop. */
  is_local: boolean;
  /** Storage account `publicNetworkAccess` (Enabled / Disabled), best-effort. */
  public_access?: string | null;
  /** `defaultAction` on the storage networkAcls (Allow / Deny). */
  default_action?: string | null;
  /** IPv4 rules currently allowlisted on the storage account. */
  ip_rules?: string[];
  /** Caller's public IP detected via ipify, or null when offline. */
  caller_ip?: string | null;
  /** True when caller_ip is already inside ip_rules. */
  caller_ip_in_rules?: boolean;
  /** Present when ARM read failed; UI may still render the toggle. */
  error?: string;
}

export interface StorageLocalDebugOpenResult {
  action: "noop" | "already_open" | "ip_added" | "opened" | "failed";
  ip?: string;
  previous_public?: string;
  off_hint?: string;
  reason?: string;
  error?: string;
}

export const storageApi = {
  /** Probe whether the dashboard should show the local-debug toggle. */
  localDebugStatus: (
    subscriptionId: string,
    resourceGroup: string,
    accountName: string,
  ) =>
    api.get<StorageLocalDebugStatus>(
      `/storage/local-debug?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(resourceGroup)}&account_name=${encodeURIComponent(accountName)}`,
    ),

  /** Open the storage account's public surface to the caller's IP (local only). */
  localDebugOpen: (
    subscriptionId: string,
    resourceGroup: string,
    accountName: string,
  ) =>
    api.post<StorageLocalDebugOpenResult>("/storage/local-debug/open", {
      subscription_id: subscriptionId,
      resource_group: resourceGroup,
      account_name: accountName,
    }),
};
