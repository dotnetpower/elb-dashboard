/**
 * Map raw `BlastJobSummary` rows from `/api/blast/jobs` into the
 * narrow `JobRowView` shape consumed by `<JobRow>`.
 *
 * The backend keeps several phase / status names alive (legacy +
 * external API + dashboard-internal). Centralising the bucketing here
 * means the bento, the modal, and any future surface render the same
 * colour for the same job — and bug-fixes happen in one place.
 */

import type { BlastJobSummary } from "@/api/endpoints";

import type { DisplayJobState, JobRowView } from "./atoms";

const ACTIVE_PENDING = new Set([
  "Pending",
  "Provisioning",
  "DownloadingDB",
  "Submitted",
  "queued",
]);

const ACTIVE_RUNNING = new Set([
  "Running",
  "InProgress",
  "Splitting",
  "running",
]);

const ACTIVE_REDUCING = new Set([
  "Reducing",
  "Aggregating",
  "Combining",
  "reducing",
]);

const COMPLETED = new Set(["Completed", "completed", "Succeeded", "succeeded", "success"]);
const FAILED = new Set(["Failed", "failed", "Error", "error", "cancelled", "Cancelled"]);

export function classifyJobState(input: {
  phase?: string;
  status?: string;
  /** Optional non-empty error string on the job row. When the
   *  phase/status fields are missing or unrecognised but the job has
   *  recorded an error, treat the row as Failed so headers, failed-rate
   *  counters and per-row chrome reflect reality instead of "Unknown". */
  error?: string | null;
}): DisplayJobState {
  const v = input.phase || input.status || "";
  if (FAILED.has(v)) return "Failed";
  if (COMPLETED.has(v)) return "Completed";
  if (ACTIVE_REDUCING.has(v)) return "Reducing";
  if (ACTIVE_RUNNING.has(v)) return "Running";
  if (ACTIVE_PENDING.has(v)) return "Pending";
  if (input.error && input.error.trim().length > 0) return "Failed";
  return v ? "Unknown" : "Pending";
}

export function isActiveJobState(s: DisplayJobState): boolean {
  return s === "Pending" || s === "Running" || s === "Reducing";
}

/** Return the cluster name a BLAST job is bound to (or null if unbound). */
export function jobClusterName(j: BlastJobSummary): string | null {
  // `infrastructure.cluster_name` is the canonical field; `payload` is
  // a legacy fallback for older rows written before the field landed.
  type WithPayload = { payload?: { cluster_name?: string } };
  const payloadCluster = (j as unknown as WithPayload).payload?.cluster_name ?? null;
  return j.infrastructure?.cluster_name ?? payloadCluster ?? null;
}

export function toJobRowView(j: BlastJobSummary): JobRowView {
  const state = classifyJobState({
    phase: j.phase,
    status: j.status,
    error: j.error,
  });
  const splitsTotal = j.splits_total ?? j.split_children?.child_count ?? 0;
  const splitsDone = j.splits_done ?? 0;
  const query = j.query_label ?? j.job_title ?? j.job_id;
  return {
    jobId: j.job_id,
    displayId: j.job_id.slice(0, 8),
    db: j.db || "—",
    query,
    state,
    createdAt: j.created_at ?? null,
    splitsDone,
    splitsTotal,
    note: j.error || null,
  };
}
