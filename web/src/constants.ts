/** Shared UI constants — single source of truth for status colors, defaults, etc. */

export const STATUS_COLORS: Record<string, string> = {
  queued: "var(--text-muted)",
  waiting_for_submit_slot: "var(--text-muted)",
  waiting_for_capacity: "var(--text-muted)",
  submitted: "var(--accent)",
  preparing: "var(--accent)",
  checking_vm: "var(--accent)",
  uploading: "var(--accent)",
  configuring: "var(--accent)",
  warmup_ready: "var(--accent)",
  waiting_for_warmup: "var(--warning)",
  warming_up: "var(--accent)",
  reading_split_query: "var(--accent)",
  splitting_queries: "var(--accent)",
  enabling_storage: "var(--accent)",
  staging_db: "var(--warning)",
  submitting: "var(--warning)",
  split_children_submitted: "var(--warning)",
  split_children_aggregating: "var(--warning)",
  running: "var(--warning)",
  exporting_results: "var(--accent)",
  split_children_merge_ready: "var(--accent)",
  split_results_waiting_for_artifacts: "var(--accent)",
  split_results_merging: "var(--accent)",
  results_pending: "var(--accent)",
  creating: "var(--warning)",
  completed: "var(--success)",
  failed: "var(--danger)",
  submit_failed: "var(--danger)",
  split_submit_invalid: "var(--danger)",
  split_results_merge_invalid: "var(--danger)",
  error: "var(--danger)",
  deleting: "var(--text-faint)",
  deleted: "var(--text-faint)",
  cancelled: "var(--warning)",
  unknown: "var(--text-faint)",
};

export function statusColor(phase: string): string {
  return STATUS_COLORS[phase] ?? "var(--text-faint)";
}

// Backend phases that the reconciler keeps with status="running" while the job
// is actually waiting in line (submit-slot lock, cluster capacity). The job list
// already collapses these to the "Queued" display state via `classifyJobState`;
// this set lets the Job Details surfaces (which render the raw phase string) show
// the same "queued" label instead of the internal phase id.
//
// This is the single source of truth for "which backend phases mean queued".
// `jobMapping.ts` imports it to build its classifier set, so adding a new
// waiting phase here flows to the badge, counts, filter, and details at once.
export const QUEUED_PHASES = new Set([
  "queued",
  "waiting_for_submit_slot",
  "waiting_for_capacity",
  "capacity_reserve_lost",
]);

/**
 * Human-friendly label for a raw backend phase string. Keeps the queued-family
 * phases (which co-occur with the status="running" reconciler sentinel) reading
 * as "queued" so the Job Details Status matches the job list badge. All other
 * phases pass through unchanged.
 */
export function phaseLabel(phase: string): string {
  if (QUEUED_PHASES.has(phase)) return "queued";
  return phase;
}

// Why a job is queued, keyed by the raw backend phase. Surfaced as the calm
// secondary line next to the QUEUED badge so the user knows whether the job is
// waiting on the submit lock, on cluster capacity, or simply sitting in the
// Celery queue — instead of an opaque "queued".
const QUEUE_REASONS: Record<string, string> = {
  waiting_for_submit_slot: "Waiting for submit slot",
  waiting_for_capacity: "Waiting for cluster capacity",
  capacity_reserve_lost: "Waiting for cluster capacity",
  queued: "Waiting in queue",
};

/**
 * Secondary explanation for a queued job's raw backend phase, or null when the
 * phase is not a queued-family phase. Used by the list row and Job Details to
 * render "why is this waiting" beneath the QUEUED badge.
 */
export function queueReasonText(phase: string | undefined | null): string | null {
  if (!phase) return null;
  return QUEUE_REASONS[phase] ?? (QUEUED_PHASES.has(phase) ? "Waiting in queue" : null);
}

/** Max FASTA file upload size in bytes (50 MB). */
export const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

/** Azure regions available for resource deployment. Sorted by recommendation. */
export const AZURE_REGIONS: readonly { value: string; label: string }[] = [
  { value: "koreacentral", label: "Korea Central (Seoul) — Recommended" },
  { value: "koreasouth", label: "Korea South (Busan)" },
  { value: "eastus", label: "East US (Virginia)" },
  { value: "eastus2", label: "East US 2 (Virginia)" },
  { value: "westus", label: "West US (California)" },
  { value: "westus2", label: "West US 2 (Washington)" },
  { value: "centralus", label: "Central US (Iowa)" },
  { value: "northeurope", label: "North Europe (Ireland)" },
  { value: "westeurope", label: "West Europe (Netherlands)" },
  { value: "southeastasia", label: "Southeast Asia (Singapore)" },
  { value: "eastasia", label: "East Asia (Hong Kong)" },
  { value: "japaneast", label: "Japan East (Tokyo)" },
  { value: "japanwest", label: "Japan West (Osaka)" },
  { value: "australiaeast", label: "Australia East (Sydney)" },
] as const;
