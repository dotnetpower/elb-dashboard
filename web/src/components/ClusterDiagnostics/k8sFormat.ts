/**
 * Pure formatting helpers for K8s node/pool rendering.
 *
 * Single-responsibility: data → display string. No React, no I/O.
 * Extracted from ClusterDiagnostics so individual sections can import
 * what they need without hauling around presentation code.
 */

const SYSTEM_POOL_HINTS = ["systempool", "system", "agentpool"];

export function isSystemPool(pool: string | undefined): boolean {
  if (!pool) return false;
  const p = pool.toLowerCase();
  return SYSTEM_POOL_HINTS.some((hint) => p === hint || p.startsWith(hint));
}

export function poolAccent(pool: string | undefined): string {
  return isSystemPool(pool) ? "var(--warning)" : "var(--accent)";
}

export function formatCores(milli: number | undefined): string {
  if (!milli || milli <= 0) return "0";
  if (milli < 1000) return (milli / 1000).toFixed(2);
  if (milli < 10_000) return (milli / 1000).toFixed(2);
  return (milli / 1000).toFixed(1);
}

export function formatMemoryGiB(ki: number | undefined): string {
  if (!ki || ki <= 0) return "0";
  const gib = ki / 1024 / 1024;
  if (gib >= 100) return gib.toFixed(0);
  if (gib >= 10) return gib.toFixed(1);
  return gib.toFixed(2);
}

export function shortNodeName(name: string): string {
  // Drop the AKS-generated prefix ("aks-<pool>-") when long enough; keep the
  // suffix that the operator actually uses to disambiguate.
  const stripped = name.replace(/^aks-/, "").replace(/-vmss/, "-");
  return stripped.length > 28 ? `…${stripped.slice(-26)}` : stripped;
}

export function pressureFlags(
  conditions: Record<string, string> | undefined,
): string[] {
  if (!conditions) return [];
  const flags: string[] = [];
  if (conditions.MemoryPressure === "True") flags.push("MemoryPressure");
  if (conditions.DiskPressure === "True") flags.push("DiskPressure");
  if (conditions.PIDPressure === "True") flags.push("PIDPressure");
  if (conditions.NetworkUnavailable === "True") flags.push("NetworkUnavailable");
  return flags;
}

/**
 * Format a K8s creationTimestamp (ISO 8601, e.g. "2026-05-27T10:00:00Z")
 * as a compact `kubectl get`-style age string: `30s`, `5m`, `2h`, `3d12h`.
 * Returns `"—"` for empty or unparseable input. Now-relative; the caller
 * is responsible for re-rendering on a timer if a live age is needed.
 */
export function formatAge(iso: string | undefined | null): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "—";
  const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m`;
  const hr = Math.floor(min / 60);
  if (hr < 24) {
    const remMin = min % 60;
    return remMin ? `${hr}h${remMin}m` : `${hr}h`;
  }
  const day = Math.floor(hr / 24);
  const remHr = hr % 24;
  return remHr ? `${day}d${remHr}h` : `${day}d`;
}

/**
 * Format a Job duration from its start/completion timestamps as a compact
 * string (`30s`, `5m`, `2h`). When `end` is empty the Job is still running,
 * so the duration is measured against now. Returns `"—"` when no start time
 * is available yet (the Job has not been scheduled).
 */
export function formatDuration(
  start: string | undefined | null,
  end: string | undefined | null,
): string {
  if (!start) return "—";
  const s = Date.parse(start);
  if (Number.isNaN(s)) return "—";
  const e = end ? Date.parse(end) : Date.now();
  const sec = Math.max(0, Math.floor(((Number.isNaN(e) ? Date.now() : e) - s) / 1000));
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  if (min < 60) {
    const remSec = sec % 60;
    return remSec ? `${min}m${remSec}s` : `${min}m`;
  }
  const hr = Math.floor(min / 60);
  const remMin = min % 60;
  return remMin ? `${hr}h${remMin}m` : `${hr}h`;
}
