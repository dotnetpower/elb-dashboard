/**
 * Pure update-availability decision for a downloaded BLAST database row.
 *
 * Why this exists: the "Update available" badge and per-row Update button must
 * agree on a single rule, and that rule has a subtle trap. The backend's
 * per-DB NCBI signature comparison is authoritative, but an EMPTY
 * `updates_available` list is ambiguous on its own — it can mean "evaluated,
 * nothing stale" OR "not evaluated (no storage scope / list failed)". The
 * `updates_available_evaluated` flag resolves the ambiguity. When the server
 * evaluated, absence from the per-DB map means "no update", full stop. Only
 * when the server did NOT evaluate may the SPA fall back to the coarse
 * `source_version !== latest_version` heuristic, which otherwise re-flags every
 * DB whenever NCBI rotates `latest-dir` (the "looks not updated" false
 * positive the user hit after a successful update).
 */

export interface DbUpdateDecisionInput {
  /** The downloaded DB's metadata (or undefined when not downloaded). */
  meta:
    | { source_version?: string | null; update_in_progress?: boolean }
    | undefined;
  /** Whether the DB is genuinely usable (passed `isBlastDbReady`). */
  isDownloaded: boolean;
  /** True when this DB name is present in the server's per-DB update map. */
  inUpdateMap: boolean;
  /** True when the backend actually ran the per-DB signature comparison. */
  updatesEvaluated: boolean;
  /** Bucket-wide NCBI `latest-dir` tag (legacy fallback only). */
  latestVersion: string | null;
}

/**
 * Returns true when the given downloaded DB should surface an "Update" action.
 */
export function dbHasUpdate({
  meta,
  isDownloaded,
  inUpdateMap,
  updatesEvaluated,
  latestVersion,
}: DbUpdateDecisionInput): boolean {
  if (!isDownloaded) return false;
  if (meta?.update_in_progress) return false;
  // Server-side per-DB signature comparison is authoritative.
  if (inUpdateMap) return true;
  // Evaluated + not in map => genuinely no update. Never second-guess it.
  if (updatesEvaluated) return false;
  // Server did not evaluate per-DB: fall back to the coarse snapshot diff.
  return (
    !!meta?.source_version &&
    !!latestVersion &&
    meta.source_version !== latestVersion
  );
}
