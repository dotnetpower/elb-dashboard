/**
 * Single source of truth for "is this BLAST database actually usable right now".
 *
 * Responsibility: Pure helpers — given a BlastDatabase / DownloadedDbMeta-shaped
 *   object, return a structured readiness verdict plus a human label. Replaces
 *   four scattered ad-hoc checks (`Boolean(db)`, `file_count > 0`,
 *   `useBlastDb.isDbReady`, manual `copy_status.phase` matches) that drifted
 *   apart and let the UI mark mid-copy DBs as ready.
 * Edit boundaries: No React, no API calls, no formatting outside what the label
 *   helper needs. The contract is `copy_status.phase === "completed"` is the
 *   ONLY value that means Ready — keep that constant in sync with the prepare-db
 *   pipeline (api/routes/storage/prepare_db.py).
 * Key entry points: `getBlastDbReadiness`, `isBlastDbReady`,
 *   `blastDbReadinessLabel`, type `BlastDbReadiness`.
 * Risky contracts: Legacy DBs (prepared before the hardening shipped) have no
 *   `copy_status` field — fall back to "has files and not mid-update".
 * Validation: `npm run --prefix web test -- --run blastDbReady`.
 */

/**
 * Minimal structural shape this util reads. Both
 * `BlastDatabase` (web/src/api/blast.ts) and
 * `DownloadedDbMeta` (web/src/components/cards/storage/useBlastDb.ts) satisfy
 * this without explicit casts thanks to TypeScript structural typing.
 */
export interface BlastDbReadinessInput {
  copy_status?: {
    phase?: string;
    success?: number;
    total_files?: number;
    failed?: number;
    pending?: number;
  };
  update_in_progress?: boolean;
  updating_to_source_version?: string | null;
  file_count?: number;
}

export type BlastDbReadinessReason =
  | "copying"
  | "partial"
  | "init_failed"
  | "cancelled"
  | "unknown_phase"
  | "updating"
  | "empty";

export type BlastDbReadiness =
  | { ready: true }
  | {
      ready: false;
      reason: BlastDbReadinessReason;
      phase?: string;
      progress?: { success: number; total: number };
      updatingTo?: string | null;
    };

const KNOWN_PHASES = new Set(["copying", "partial", "init_failed", "cancelled"]);

/**
 * Authoritative readiness verdict. Modern entries (post-hardening) carry
 * `copy_status.phase` and only "completed" counts. Legacy entries fall back to
 * "has files and not mid-update" so old DBs prepared before the hardening
 * shipped keep working.
 */
export function getBlastDbReadiness(
  db: BlastDbReadinessInput | null | undefined,
): BlastDbReadiness {
  if (!db) return { ready: false, reason: "empty" };
  const copy = db.copy_status;
  const phase = copy?.phase ? String(copy.phase) : undefined;
  if (phase) {
    if (phase === "completed") return { ready: true };
    const success = Number(copy?.success ?? 0);
    const total = Number(copy?.total_files ?? 0);
    const progress = total > 0 ? { success, total } : undefined;
    const reason: BlastDbReadinessReason = KNOWN_PHASES.has(phase)
      ? (phase as BlastDbReadinessReason)
      : "unknown_phase";
    return { ready: false, reason, phase, progress };
  }
  if (db.update_in_progress) {
    return {
      ready: false,
      reason: "updating",
      updatingTo: db.updating_to_source_version ?? null,
    };
  }
  if (db.file_count && db.file_count > 0) return { ready: true };
  return { ready: false, reason: "empty" };
}

/** Boolean shorthand. Equivalent to `getBlastDbReadiness(db).ready`. */
export function isBlastDbReady(
  db: BlastDbReadinessInput | null | undefined,
): boolean {
  return getBlastDbReadiness(db).ready;
}

/**
 * Short human label for status pills. Returns "Storage DB ready" for the ready
 * case so the existing Warmup row markup keeps its current text.
 */
export function blastDbReadinessLabel(r: BlastDbReadiness): string {
  if (r.ready) return "Storage DB ready";
  switch (r.reason) {
    case "copying":
      return r.progress
        ? `Downloading · ${r.progress.success}/${r.progress.total} files`
        : "Downloading…";
    case "partial":
      return r.progress
        ? `Partial copy · ${r.progress.success}/${r.progress.total} files`
        : "Partial copy";
    case "init_failed":
      return "Copy init failed";
    case "cancelled":
      return "Download cancelled";
    case "updating":
      return r.updatingTo ? `Updating to ${r.updatingTo}` : "Updating DB generation";
    case "unknown_phase":
      return r.phase ? `Phase: ${r.phase}` : "Not ready";
    case "empty":
    default:
      return "Storage DB not ready";
  }
}

/**
 * Tone hint for `StatusPill` — keeps the visual language consistent with
 * existing warmup pills (`ok`/`accent`/`neutral`) plus a new `loading` /
 * `blocked` for in-flight / failed states. The Warmup pill renderer maps these
 * to colors; callers outside that renderer can ignore the value.
 */
export type BlastDbReadinessTone =
  | "ok"
  | "loading"
  | "blocked"
  | "neutral"
  | "accent";

export function blastDbReadinessTone(r: BlastDbReadiness): BlastDbReadinessTone {
  if (r.ready) return "ok";
  switch (r.reason) {
    case "copying":
    case "updating":
      return "loading";
    case "partial":
    case "init_failed":
      return "blocked";
    case "cancelled":
    case "unknown_phase":
    case "empty":
    default:
      return "neutral";
  }
}

/** Short reason code suitable for `submitValidation` blocked-action messages. */
export function blastDbBlockedReason(r: BlastDbReadiness): string | null {
  if (r.ready) return null;
  switch (r.reason) {
    case "copying":
      return r.progress
        ? `Download in progress (${r.progress.success}/${r.progress.total} files). Wait for it to complete.`
        : "Download in progress. Wait for it to complete.";
    case "partial":
      return "Last download did not complete. Retry from the Storage card before searching.";
    case "init_failed":
      return "Copy initiation failed. Retry from the Storage card before searching.";
    case "cancelled":
      return "Download was cancelled. Restart it from the Storage card.";
    case "updating":
      return r.updatingTo
        ? `DB is updating to ${r.updatingTo}. Wait for the update to finish.`
        : "DB is updating. Wait for the update to finish.";
    case "unknown_phase":
      return r.phase ? `DB phase is ${r.phase}; not ready for search.` : "DB is not ready.";
    case "empty":
    default:
      return "Storage DB is not ready.";
  }
}
