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

type UnknownRecord = Record<string, unknown>;

const STALE_ACTIVE_WITHOUT_PROGRESS_MS = 30 * 60 * 1000;

const ACTIVE_PENDING = new Set([
  "Pending",
  "Provisioning",
  "DownloadingDB",
  "Submitted",
  "submitted",
  "submitting",
  "preparing",
  "queued",
  "waiting_for_warmup",
]);

const ACTIVE_RUNNING = new Set(["Running", "InProgress", "Splitting", "running"]);

const ACTIVE_REDUCING = new Set(["Reducing", "Aggregating", "Combining", "reducing"]);

const COMPLETED = new Set([
  "Completed",
  "completed",
  "Succeeded",
  "succeeded",
  "success",
]);
const FAILED = new Set([
  "Failed",
  "failed",
  "Error",
  "error",
  "submit_failed",
  "config_invalid",
  "warmup_not_ready",
  "split_submit_invalid",
  "cancelled",
  "Cancelled",
]);

function classifyValue(value: string | undefined): DisplayJobState | null {
  if (!value) return null;
  if (FAILED.has(value)) return "Failed";
  if (COMPLETED.has(value)) return "Completed";
  if (ACTIVE_REDUCING.has(value)) return "Reducing";
  if (ACTIVE_RUNNING.has(value)) return "Running";
  if (ACTIVE_PENDING.has(value)) return "Pending";
  return null;
}

export function classifyJobState(input: {
  phase?: string;
  status?: string;
  /** Optional non-empty error string on the job row. When the
   *  phase/status fields are missing or unrecognised but the job has
   *  recorded an error, treat the row as Failed so headers, failed-rate
   *  counters and per-row chrome reflect reality instead of "Unknown". */
  error?: string | null;
}): DisplayJobState {
  const phaseState = classifyValue(input.phase);
  const statusState = classifyValue(input.status);
  if (phaseState === "Failed" || statusState === "Failed") return "Failed";
  if (phaseState === "Completed" || statusState === "Completed") return "Completed";
  if (statusState) return statusState;
  if (phaseState) return phaseState;
  if (input.error && input.error.trim().length > 0) return "Failed";
  const v = input.phase || input.status || "";
  return v ? "Unknown" : "Pending";
}

export function isActiveJobState(s: DisplayJobState): boolean {
  return s === "Pending" || s === "Running" || s === "Reducing";
}

export function jobDisplayState(j: BlastJobSummary): DisplayJobState {
  return toJobRowView(j).state;
}

export function isDashboardJobActive(j: BlastJobSummary): boolean {
  return isActiveJobState(jobDisplayState(j));
}

export function isDashboardJobCompleted(j: BlastJobSummary): boolean {
  return jobDisplayState(j) === "Completed";
}

export function isDashboardJobFailed(j: BlastJobSummary): boolean {
  return jobDisplayState(j) === "Failed";
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
  const execution = externalExecution(j);
  const splitsTotal =
    j.splits_total ?? j.split_children?.child_count ?? execution.total ?? 0;
  const splitsDone = j.splits_done ?? execution.done ?? 0;
  const stale = isStaleActiveWithoutProgress(j, state, splitsTotal);
  const effectiveState = stale ? "Unknown" : state;
  const query = j.query_label ?? externalQueryLabel(j) ?? "";
  const title = j.job_title || fallbackJobTitle(j, query);
  const note = stale
    ? "Stale state: no live execution signal"
    : j.error || externalErrorMessage(j) || null;
  return {
    jobId: j.job_id,
    displayId: j.job_id.slice(0, 8),
    title,
    db: shortDbLabel(j.db || externalDbLabel(j) || "—"),
    query,
    state: effectiveState,
    createdAt: j.created_at ?? null,
    elapsedSec: terminalElapsedSec(j, effectiveState),
    splitsDone,
    splitsTotal,
    note,
  };
}

function fallbackJobTitle(j: BlastJobSummary, query: string): string {
  const db = shortDbLabel(j.db || externalDbLabel(j) || "");
  const parts = [j.program || "blast", db, query].filter(Boolean);
  return parts.length > 0 ? parts.join(" - ") : j.job_id;
}

function asRecord(value: unknown): UnknownRecord | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as UnknownRecord)
    : null;
}

function numberFrom(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return Math.max(0, value);
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? Math.max(0, parsed) : null;
  }
  return null;
}

function externalPayload(j: BlastJobSummary): UnknownRecord | null {
  return asRecord(asRecord(j.payload)?.external);
}

function externalExecution(j: BlastJobSummary): { done?: number; total?: number } {
  const output = asRecord(j.output);
  const outputExecution = asRecord(output?.execution);
  const payloadExecution = asRecord(externalPayload(j)?.execution);
  const execution = outputExecution ?? payloadExecution;
  if (!execution) return {};
  const total = numberFrom(execution.shard_count) ?? undefined;
  const succeeded = numberFrom(execution.shards_succeeded) ?? 0;
  const failed = numberFrom(execution.shards_failed) ?? 0;
  const done = total == null ? succeeded + failed : Math.min(total, succeeded + failed);
  return { done, total };
}

function externalQueryLabel(j: BlastJobSummary): string | null {
  const external = externalPayload(j);
  return basename(external?.query_file ?? external?.query ?? external?.query_blob_url);
}

function externalDbLabel(j: BlastJobSummary): string | null {
  const external = externalPayload(j);
  return basename(external?.db_name ?? external?.db);
}

function externalErrorMessage(j: BlastJobSummary): string | null {
  const error = asRecord(externalPayload(j)?.error);
  if (!error) return null;
  const message = error.message;
  return typeof message === "string" && message.trim() ? message.trim() : null;
}

function basename(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const raw = value.trim();
  if (!raw) return null;
  try {
    const parsed = raw.startsWith("az://")
      ? new URL(`https://${raw.slice("az://".length)}`)
      : new URL(raw);
    const tail = parsed.pathname.split("/").filter(Boolean).pop();
    if (tail) return tail;
  } catch {
    // Not a URL; fall through to generic path handling.
  }
  return raw.replace(/\\/g, "/").split("/").filter(Boolean).pop() ?? raw;
}

function shortDbLabel(value: string): string {
  return basename(value) ?? value;
}

function isStaleActiveWithoutProgress(
  j: BlastJobSummary,
  state: DisplayJobState,
  splitsTotal: number,
): boolean {
  if (!isActiveJobState(state) || splitsTotal > 0) return false;
  const timestamp = Date.parse(j.updated_at || j.created_at || "");
  if (!Number.isFinite(timestamp)) return false;
  return Date.now() - timestamp > STALE_ACTIVE_WITHOUT_PROGRESS_MS;
}

function terminalElapsedSec(j: BlastJobSummary, state: DisplayJobState): number | null {
  if (isActiveJobState(state)) return null;
  const created = Date.parse(j.created_at || "");
  const updated = Date.parse(j.updated_at || "");
  if (!Number.isFinite(created) || !Number.isFinite(updated) || updated < created) {
    return null;
  }
  return Math.max(0, Math.floor((updated - created) / 1000));
}
