/**
 * Typed client for `/api/upgrade/*`.
 *
 * Mirrors the response shapes in `api/routes/upgrade.py`. Renaming a
 * field here without a coordinated backend change will break the SPA's
 * upgrade page.
 *
 * Endpoints:
 *  - GET    /upgrade/status                     (any caller)
 *  - GET    /upgrade/candidates                 (any caller)
 *  - POST   /upgrade/check                      (any caller; throttled)
 *  - GET    /upgrade/history?limit=N            (any caller)
 *  - POST   /upgrade/start                      (UpgradeAdmin)
 *  - POST   /upgrade/rollback                   (UpgradeAdmin)
 *  - GET    /upgrade/escape-hatch               (UpgradeAdmin)
 *  - GET    /upgrade/jobs/{job_id}/build-log/{component} (UpgradeAdmin)
 */

import { api, fetchApiRaw } from "@/api/client";

export type UpgradeStateName =
  | "idle"
  | "checking"
  | "queued"
  | "fetching"
  | "building"
  | "patching"
  | "rolling_out"
  | "succeeded"
  | "failed_pre"
  | "failed_rollout"
  | "rolling_back"
  | "rolled_back"
  | "rollback_failed";

export interface UpgradeStatus {
  running_version: string;
  running_sha: string;
  running_revision: string;
  current_images: Record<string, string>;
  latest_version: string;
  latest_sha: string;
  latest_checked_at: string;
  git_remote: string;
  state: UpgradeStateName;
  target_version: string;
  target_sha: string;
  job_id: string;
  started_by_oid: string;
  started_at: string;
  phase_detail: string;
  phase_progress: number;
  build_log_blob: string;
  rollback_target: Record<string, string>;
  rollback_available_until: string;
  updated_at: string;
}

export interface UpgradeCandidate {
  name: string;
  raw_ref: string;
  commit_sha: string;
}

export interface UpgradeCandidatesResponse {
  configured: boolean;
  remote: string | null;
  running_version: string;
  candidates: UpgradeCandidate[];
  error?: string;
}

export interface UpgradeStartRequest {
  target_version: string;
  target_sha?: string;
  confirm_downtime: boolean;
}

export interface UpgradeHistoryEvent {
  ts: string;
  job_id: string;
  event: string;
  [key: string]: unknown;
}

export interface UpgradeEscapeHatch {
  container_app: string;
  subscription_id: string;
  resource_group: string;
  target_images: Record<string, string>;
  commands: string[];
}

export interface UpgradeRollbackPreflightImage {
  image_ref: string;
  exists: boolean;
  created_on: string | null;
  error: string;
}

export interface UpgradeRollbackPreflight {
  available: boolean;
  reason: string;
  images: UpgradeRollbackPreflightImage[];
}

export const upgradeApi = {
  status: () => api.get<UpgradeStatus>("/upgrade/status"),
  candidates: () => api.get<UpgradeCandidatesResponse>("/upgrade/candidates"),
  check: () => api.post<UpgradeStatus>("/upgrade/check", {}),
  start: (body: UpgradeStartRequest) => api.post<UpgradeStatus>("/upgrade/start", body),
  rollback: () => api.post<UpgradeStatus>("/upgrade/rollback", {}),
  rollbackPreflight: () => api.get<UpgradeRollbackPreflight>("/upgrade/rollback-preflight"),
  escapeHatch: () => api.get<UpgradeEscapeHatch>("/upgrade/escape-hatch"),
  history: (limit = 50) =>
    api.get<{ events: UpgradeHistoryEvent[] }>(`/upgrade/history?limit=${limit}`),
  buildLog: async (jobId: string, component: string): Promise<string> => {
    const resp = await fetchApiRaw(`/upgrade/jobs/${jobId}/build-log/${component}`);
    if (!resp.ok) {
      throw new Error(`build log ${resp.status}`);
    }
    return resp.text();
  },
};

/**
 * Return true when `latest_version` is strictly greater than `running_version`
 * using a simple semver-ish comparison. The SPA badge polls /status often;
 * keeping the comparison local avoids a second round trip.
 */
export function isUpgradeAvailable(status: UpgradeStatus | null | undefined): boolean {
  if (!status) return false;
  if (!status.latest_version || !status.running_version) return false;
  return compareSemver(status.latest_version, status.running_version) > 0;
}

export function compareSemver(a: string, b: string): number {
  const aa = a.split(".").map((n) => parseInt(n, 10) || 0);
  const bb = b.split(".").map((n) => parseInt(n, 10) || 0);
  const len = Math.max(aa.length, bb.length);
  for (let i = 0; i < len; i += 1) {
    const x = aa[i] ?? 0;
    const y = bb[i] ?? 0;
    if (x !== y) return x - y;
  }
  return 0;
}

/**
 * Buckets a state into a high-level phase the badge uses to pick colour.
 * "active" includes any post-start non-terminal state.
 */
export function statePhase(
  state: UpgradeStateName,
): "idle" | "active" | "succeeded" | "failed" | "rolled_back" {
  switch (state) {
    case "idle":
      return "idle";
    case "succeeded":
      return "succeeded";
    case "rolled_back":
      return "rolled_back";
    case "failed_pre":
    case "failed_rollout":
    case "rollback_failed":
      return "failed";
    default:
      return "active";
  }
}
