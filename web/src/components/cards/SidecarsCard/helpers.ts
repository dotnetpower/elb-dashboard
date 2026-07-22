import type { SidecarsSnapshot } from "@/hooks/useSidecarMetrics";

export function rollupStatus(
  snap: SidecarsSnapshot | undefined,
): "ok" | "loading" | "error" | "unavailable" {
  if (!snap || !snap.sidecars || Object.keys(snap.sidecars).length === 0)
    return "loading";
  const list = Object.values(snap.sidecars);
  if (list.some((s) => s.health === "down")) return "error";
  if (list.some((s) => s.health === "degraded")) return "unavailable";
  return "ok";
}

export function summary(snap: SidecarsSnapshot | undefined): string {
  if (!snap) return "—";
  const list = Object.values(snap.sidecars);
  const ok = list.filter((s) => s.health === "ok").length;
  return `${ok}/${list.length} healthy`;
}

/** Compact absolute-byte label for the tiny topology footer (e.g. "128M"). */
export function formatMemBytesCompact(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)}K`;
  if (bytes < 1024 * 1024 * 1024) return `${Math.round(bytes / (1024 * 1024))}M`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)}G`;
}

/**
 * Memory label for the sidecar topology footer.
 *
 * Container Apps sidecars usually run without a cgroup `memory.max` limit, so
 * the reporter cannot compute a percentage and leaves `mem_pct` null. Rather
 * than render an empty "mem —", fall back to the absolute `mem_bytes` the
 * reporter always writes. Returns "—" only when neither is available (e.g. a
 * stale snapshot, which clears both).
 */
export function memLabel(memPct?: number | null, memBytes?: number): string {
  if (memPct != null && Number.isFinite(memPct)) return `${memPct}%`;
  if (memBytes != null && Number.isFinite(memBytes))
    return formatMemBytesCompact(memBytes);
  return "—";
}

export function staleSnapshot(
  snap: SidecarsSnapshot | undefined,
): SidecarsSnapshot | undefined {
  if (!snap) return snap;
  return {
    ...snap,
    degraded: true,
    degraded_reason: "sidecar snapshot is stale",
    sidecars: Object.fromEntries(
      Object.entries(snap.sidecars).map(([key, sidecar]) => [
        key,
        {
          ...sidecar,
          health: "degraded" as const,
          cpu_pct: undefined,
          mem_pct: undefined,
          mem_bytes: undefined,
        },
      ]),
    ),
  };
}
