// Helpers shared with ClusterDiagnostics; kept duplicated here on purpose
// so the card body can render a compact summary without importing the
// whole diagnostics module (and pulling its tree-shake exclusions).

const SYSTEM_POOL_HINTS = ["systempool", "system", "agentpool"];

export function isSystemPool(pool: string | undefined): boolean {
  if (!pool) return false;
  const p = pool.toLowerCase();
  return SYSTEM_POOL_HINTS.some((h) => p === h || p.startsWith(h));
}

export function fmtCores(milli: number): string {
  if (milli <= 0) return "0";
  if (milli < 10_000) return (milli / 1000).toFixed(2);
  return (milli / 1000).toFixed(1);
}

export function fmtGiB(ki: number): string {
  if (ki <= 0) return "0";
  const gib = ki / 1024 / 1024;
  if (gib >= 100) return gib.toFixed(0);
  if (gib >= 10) return gib.toFixed(1);
  return gib.toFixed(2);
}
