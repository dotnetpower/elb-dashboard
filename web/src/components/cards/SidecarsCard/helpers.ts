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
        },
      ]),
    ),
  };
}
