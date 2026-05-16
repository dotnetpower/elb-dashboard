/** Shared UI constants — single source of truth for status colors, defaults, etc. */

export const STATUS_COLORS: Record<string, string> = {
  submitted: "var(--accent)",
  checking_vm: "var(--accent)",
  uploading: "var(--accent)",
  configuring: "var(--accent)",
  reading_split_query: "var(--accent)",
  splitting_queries: "var(--accent)",
  enabling_storage: "var(--accent)",
  submitting: "var(--warning)",
  split_children_submitted: "var(--warning)",
  split_children_aggregating: "var(--warning)",
  running: "var(--warning)",
  exporting_results: "var(--accent)",
  split_children_merge_ready: "var(--accent)",
  split_results_waiting_for_artifacts: "var(--accent)",
  split_results_merging: "var(--accent)",
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
