/**
 * ClusterPulse — pure formatting + tone helpers.
 *
 * Kept side-effect free so the leaf UI modules (atoms, JobLine) can
 * import them without dragging in data hooks. Anything that needs
 * React state lives in `usePulseSignals` / `useClusterHealth`.
 */

import type { DisplayJobState } from "@/components/cards/ClusterBento/jobTypes";

export type HealthTone =
  | "healthy"
  | "degraded"
  | "down"
  | "transitioning"
  | "unknown";

export function toneColor(tone: HealthTone): string {
  switch (tone) {
    case "healthy":
      return "var(--success)";
    case "degraded":
      return "var(--warning)";
    case "down":
      return "var(--danger)";
    case "transitioning":
      return "var(--accent)";
    case "unknown":
      return "var(--text-muted)";
  }
}

export function jobStateTone(state: DisplayJobState): string {
  switch (state) {
    case "Running":
      return "var(--accent)";
    case "Reducing":
      return "var(--teal)";
    case "Completed":
      return "var(--success)";
    case "Pending":
      return "var(--text-faint)";
    case "Failed":
      return "var(--danger)";
    case "Unknown":
      return "var(--text-muted)";
  }
}

export function jobTimeText(
  state: DisplayJobState,
  elapsedSec: number,
  etaSec: number | null | undefined,
): string {
  if (state === "Completed") return `done in ${fmtSec(elapsedSec)}`;
  if (state === "Failed") return `failed @ ${fmtSec(elapsedSec)}`;
  if (state === "Pending") return "queued";
  if (etaSec != null) return `${fmtSec(elapsedSec)} · ETA ${fmtSec(etaSec)}`;
  return fmtSec(elapsedSec);
}

export function noteToneFor(note: string | null | undefined): string {
  if (!note) return "var(--text-faint)";
  const low = note.toLowerCase();
  if (low.includes("oomkilled") || low.includes("unschedulable"))
    return "var(--danger)";
  if (low.startsWith("slow") || low.startsWith("stalled"))
    return "var(--warning)";
  return "var(--text-faint)";
}

export function fmtMs(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}

export function fmtSec(sec: number): string {
  if (sec <= 0) return "0s";
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m < 60) return s === 0 ? `${m}m` : `${m}m${s}s`;
  const h = Math.floor(m / 60);
  return `${h}h${(m % 60).toString().padStart(2, "0")}m`;
}

/** Return the local-part of an UPN ("alice@contoso.com" → "alice"), or
 *  null if there is no UPN to render. Used for the per-job submitter
 *  affordance on the cluster pulse. */
export function ownerLabel(upn: string | null | undefined): string | null {
  if (!upn) return null;
  const at = upn.indexOf("@");
  return at > 0 ? upn.slice(0, at) : upn;
}

/** Strip the `queries/uploads/<uuid>/` storage prefix from a query
 *  label so the table shows just the filename the user uploaded.
 *  Falls back to the original string when it does not match the
 *  prefix shape. */
export function prettifyQueryLabel(raw: string): string {
  if (!raw) return raw;
  // Matches: queries/uploads/<uuid-ish>/<basename>
  const m = raw.match(
    /^queries\/uploads\/[0-9a-fA-F-]{6,}\/(.+)$/,
  );
  if (m) return m[1];
  // Generic fallback: take the last path segment when the value looks
  // like a storage path so users see "query.fa" instead of the prefix.
  if (raw.includes("/")) {
    const tail = raw.split("/").pop();
    if (tail) return tail;
  }
  return raw;
}

/** Compress a raw error message into a single short line: drop the
 *  redundant "ERROR:" prefix the backend prepends and clamp to roughly
 *  one card-width worth of characters. The full message is still shown
 *  on hover via the JobLine's `title` attribute. */
export function summariseNote(
  note: string | null | undefined,
  max = 80,
): string | null {
  if (!note) return null;
  let s = note.trim();
  // Collapse newlines / runs of whitespace.
  s = s.replace(/\s+/g, " ");
  s = s.replace(/^ERROR:\s*/i, "");
  if (s.length > max) s = `${s.slice(0, max - 1).trimEnd()}…`;
  return s;
}

/** Estimate seconds remaining for a Running job based on its splits
 *  progress. Returns null when we don't have enough signal (Pending,
 *  no splits, no elapsed). Used by JobLine when the backend has not
 *  yet pushed an explicit `etaSec`. */
export function estimateEtaSec(args: {
  elapsedSec: number;
  splitsDone: number;
  splitsTotal: number;
}): number | null {
  const { elapsedSec, splitsDone, splitsTotal } = args;
  if (splitsTotal <= 0 || splitsDone <= 0) return null;
  if (splitsDone >= splitsTotal) return null;
  if (elapsedSec < 5) return null;
  const perSplit = elapsedSec / splitsDone;
  return Math.max(1, Math.round(perSplit * (splitsTotal - splitsDone)));
}
