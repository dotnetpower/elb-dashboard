/** Shared UI constants — single source of truth for status colors, defaults, etc. */

export const STATUS_COLORS: Record<string, string> = {
  submitted: "var(--accent)",
  uploading: "var(--accent)",
  configuring: "var(--accent)",
  enabling_storage: "var(--accent)",
  submitting: "var(--warning)",
  running: "var(--warning)",
  creating: "var(--warning)",
  completed: "var(--success)",
  failed: "var(--danger)",
  deleting: "var(--text-faint)",
  deleted: "var(--text-faint)",
  unknown: "var(--text-faint)",
};

export function statusColor(phase: string): string {
  return STATUS_COLORS[phase] ?? "var(--text-faint)";
}

/** Max FASTA file upload size in bytes (50 MB). */
export const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;
