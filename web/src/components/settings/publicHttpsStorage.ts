/**
 * localStorage persistence for the in-flight Public HTTPS enable/disable task.
 *
 * Extracted verbatim from `SettingsPanel.tsx` (issue #24 SRP split). Pure
 * storage helpers with no React/JSX dependency: they let `PublicHttpsSection`
 * resume its progress badge after a tab switch without re-triggering the
 * Enable action. Keyed per cluster (v2) with transparent migration of the
 * legacy single-slot (v1) entry.
 */

export interface RunningPublicHttpsTask {
  taskId: string;
  startedAt: number;
  cluster: string;
  kind: "enable" | "disable";
}

// localStorage key carries the cluster so the operator can fire Enable
// against two clusters back-to-back without the second one overwriting
// the first's progress badge. Bumped to v2 when the cluster suffix
// was introduced; legacy v1 entries (a single global slot) are migrated
// transparently on first read.
const PUBLIC_HTTPS_TASK_STORAGE_PREFIX = "elb.publicHttps.runningTask.v2";
const PUBLIC_HTTPS_LEGACY_KEY = "elb.publicHttps.runningTask.v1";
// 30 min ceiling — first-time install averages 3-5 min and the longest
// observed run was ~12 min on a cold AKS cluster. A leftover record older
// than this is almost certainly a worker crash and would just block the
// Enable button forever, so we drop it on read.
const PUBLIC_HTTPS_TASK_MAX_AGE_MS = 30 * 60 * 1000;

function _storageKeyForCluster(cluster: string): string {
  // RowKey-style sanitisation in case a cluster name ever contains
  // characters Web Storage does not love (none today, future-proofing).
  return `${PUBLIC_HTTPS_TASK_STORAGE_PREFIX}.${cluster.trim() || "_default"}`;
}

function _parseTask(raw: string | null): RunningPublicHttpsTask | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<RunningPublicHttpsTask> | null;
    if (!parsed || typeof parsed !== "object") return null;
    const taskId = typeof parsed.taskId === "string" ? parsed.taskId : "";
    const startedAt = typeof parsed.startedAt === "number" ? parsed.startedAt : 0;
    if (!taskId || !startedAt) return null;
    if (Date.now() - startedAt > PUBLIC_HTTPS_TASK_MAX_AGE_MS) return null;
    const cluster = typeof parsed.cluster === "string" ? parsed.cluster : "";
    const kind: RunningPublicHttpsTask["kind"] = parsed.kind === "disable" ? "disable" : "enable";
    return { taskId, startedAt, cluster, kind };
  } catch {
    return null;
  }
}

export function loadRunningPublicHttpsTask(cluster?: string): RunningPublicHttpsTask | null {
  try {
    if (cluster) {
      const direct = _parseTask(window.localStorage.getItem(_storageKeyForCluster(cluster)));
      if (direct) return direct;
    }
    // Fall back: scan every v2 key + the legacy v1 single-slot entry and
    // pick the freshest non-expired record. This covers two cases:
    //   1. The component mounts before cluster discovery completes so
    //      the caller passes "" for cluster.
    //   2. A previous build wrote to the legacy v1 slot — we want to
    //      still show its progress instead of silently re-enabling Enable.
    let best: RunningPublicHttpsTask | null = null;
    for (let i = 0; i < window.localStorage.length; i++) {
      const key = window.localStorage.key(i);
      if (!key) continue;
      if (key !== PUBLIC_HTTPS_LEGACY_KEY && !key.startsWith(`${PUBLIC_HTTPS_TASK_STORAGE_PREFIX}.`)) {
        continue;
      }
      const candidate = _parseTask(window.localStorage.getItem(key));
      if (!candidate) {
        // Clean up expired / malformed rows opportunistically.
        try {
          window.localStorage.removeItem(key);
        } catch {
          // ignore
        }
        continue;
      }
      if (!best || candidate.startedAt > best.startedAt) {
        best = candidate;
      }
    }
    return best;
  } catch {
    return null;
  }
}

export function saveRunningPublicHttpsTask(task: RunningPublicHttpsTask): void {
  try {
    window.localStorage.setItem(
      _storageKeyForCluster(task.cluster),
      JSON.stringify(task),
    );
  } catch {
    // Quota / private-window failures are harmless — we just lose the
    // resume-after-tab-switch convenience for this run.
  }
}

export function clearRunningPublicHttpsTask(cluster: string): void {
  try {
    window.localStorage.removeItem(_storageKeyForCluster(cluster));
    // Best-effort sweep of the legacy single-slot key so the SPA never
    // ends up looking at stale v1 state after the operator clicks
    // Disable for the new-format cluster.
    window.localStorage.removeItem(PUBLIC_HTTPS_LEGACY_KEY);
  } catch {
    // ignore
  }
}
