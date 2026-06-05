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
 *  - POST   /upgrade/settings                   (any caller; channel toggle)
 *  - GET    /upgrade/history?limit=N            (any caller)
 *  - POST   /upgrade/start                      (UpgradeAdmin: Owner/Contributor RBAC, app role, or allowlist)
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
  | "validating"
  | "confirming"
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
  /** Tracking-branch HEAD commit sha (40-hex) when the commit channel is on; else "". */
  latest_commit_sha: string;
  git_remote: string;
  /** Update channel: when true, new commits on the tracking branch are surfaced too. */
  track_commits: boolean;
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
  // Blue/green (STRICT_BLUEGREEN) staging fields. Empty string when the
  // Single-mode recreate path ran (flag off) or the row is idle.
  green_revision: string;
  blue_revision: string;
  confirm_deadline: string;
  traffic_serving: string;
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
  target_version?: string;
  target_sha?: string;
  /** "release" (a vX.Y.Z tag, default) or "commit" (latest tracking-branch commit). */
  target_kind?: "release" | "commit";
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
  setTrackCommits: (trackCommits: boolean) =>
    api.post<UpgradeStatus>("/upgrade/settings", { track_commits: trackCommits }),
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

/**
 * Return true when the commit channel is on and the tracking-branch HEAD
 * commit differs from the running commit. `runningCommit` is the build-time
 * commit stamp (`__APP_COMMIT__`), which may be a 7-char short sha while
 * `latest_commit_sha` is the full 40-hex from the remote, so the comparison
 * is prefix-based. Returns false when either side is unknown.
 */
export function isCommitUpdateAvailable(
  status: UpgradeStatus | null | undefined,
  runningCommit: string | null | undefined,
): boolean {
  if (!status || !status.track_commits) return false;
  const latest = (status.latest_commit_sha || "").toLowerCase();
  const running = (runningCommit || "").toLowerCase();
  if (!latest || running.length < 7 || running === "dev" || running === "unknown") {
    return false;
  }
  return !latest.startsWith(running);
}

/**
 * Build a GitHub "compare" URL (`<base>/compare/<from>...<to>`) for the range
 * between the running build and the latest discovered ref, so an operator can
 * read exactly which commits an update would bring in.
 *
 * Returns `null` unless the remote is a GitHub HTTPS/SSH `.git` URL and both
 * endpoints of the range are known. The "to" end prefers the full
 * `latest_commit_sha` (commit channel) and falls back to `latest_sha` (the
 * release tag's commit); the "from" end is the running commit stamp.
 * Credentials in the remote URL (if any) are stripped.
 */
export function githubCompareUrl(
  status: UpgradeStatus | null | undefined,
  runningCommit: string | null | undefined,
): string | null {
  if (!status) return null;
  const base = githubRepoBaseUrl(status.git_remote);
  if (!base) return null;
  const from = (runningCommit || status.running_sha || "").trim();
  const to = (status.latest_commit_sha || status.latest_sha || "").trim();
  if (!from || !to) return null;
  if (from.toLowerCase() === to.toLowerCase()) return null;
  return `${base}/compare/${encodeURIComponent(from)}...${encodeURIComponent(to)}`;
}

/**
 * Normalise a GitHub remote (`https://github.com/owner/repo.git`,
 * `git@github.com:owner/repo.git`, with or without a trailing `.git`, and any
 * embedded credentials) to its canonical browseable base
 * `https://github.com/owner/repo`. Returns `null` for non-GitHub or
 * unparseable remotes.
 */
export function githubRepoBaseUrl(remote: string | null | undefined): string | null {
  const raw = (remote || "").trim();
  if (!raw) return null;
  // Match `owner/repo` after a github.com host in either HTTPS or SCP-like SSH.
  const match = /github\.com[/:]+([^/\s]+)\/([^/\s]+?)(?:\.git)?\/?$/i.exec(raw);
  if (!match) return null;
  const [, owner, repo] = match;
  if (!owner || !repo) return null;
  return `https://github.com/${owner}/${repo}`;
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
