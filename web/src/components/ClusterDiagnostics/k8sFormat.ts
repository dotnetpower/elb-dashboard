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
