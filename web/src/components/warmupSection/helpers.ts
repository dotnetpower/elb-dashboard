/**
 * WarmupSection helpers — pure data shaping for the warmup panel.
 *
 * Responsibility: Pure helpers + candidate list + row/capacity types
 *   used by [WarmupSection.tsx](../WarmupSection.tsx). No React, no API
 *   calls, no toasts.
 * Edit boundaries: Keep this file React-free. UI strings live here only
 *   when they describe a phase / progress label that the component renders
 *   verbatim.
 * Key entry points: `WARMUP_CANDIDATES`, `summariseWarmupCapacity`,
 *   `buildWarmupRows`, `formatWarmupProgress`, `shortWarmupPhase`,
 *   `formatPhaseCounts`, `formatDuration`, `formatBytes`, types
 *   `WarmupCapacity` and `WarmupRow`.
 * Risky contracts: `WARMUP_CANDIDATES.value` strings must stay in sync
 *   with the backend warmup planner's known database names; changing
 *   them silently hides a card from the dashboard.
 * Validation: `npm run --prefix web test --run` covers `buildWarmupRows`
 *   and `formatWarmupProgress` indirectly through the WarmupSection
 *   render tests.
 */
import type { K8sNodeMetrics, WarmupDbInfo } from "@/api/endpoints";
import type { BlastDatabase, BlastWarmupPlan } from "@/api/blast";
import { isSystemPool } from "@/components/ClusterDetailModal/k8sFormat";

export const WARMUP_CANDIDATES = [
  {
    value: "16S_ribosomal_RNA",
    label: "16S ribosomal RNA",
    program: "blastn",
    size: "~18 MB",
  },
  {
    value: "18S_fungal_sequences",
    label: "18S fungal sequences",
    program: "blastn",
    size: "~3 MB",
  },
  {
    value: "ITS_RefSeq_Fungi",
    label: "ITS RefSeq Fungi",
    program: "blastn",
    size: "~8 MB",
  },
  { value: "pdbnt", label: "PDB nucleotide", program: "blastn", size: "~200 MB" },
  { value: "swissprot", label: "SwissProt", program: "blastp", size: "~300 MB" },
  { value: "core_nt", label: "Core nucleotide", program: "blastn", size: "~250 GB" },
  { value: "nt", label: "Nucleotide collection", program: "blastn", size: "~400 GB" },
  { value: "nr", label: "Non-redundant protein", program: "blastp", size: "~300 GB" },
  {
    value: "refseq_protein",
    label: "RefSeq protein",
    program: "blastp",
    size: "~100 GB",
  },
] as const;

export interface WarmupCapacity {
  nodes: number;
  memoryPct: number | null;
  minFreeGiB: number | null;
  memoryPressure: boolean;
  pressureFlags: string[];
}

export interface WarmupRow {
  name: string;
  label: string;
  sizeLabel: string;
  storageLabel: string;
  shardLabel: string;
  cacheLabel: string;
  cacheTone: "ready" | "loading" | "blocked" | "pressure" | "neutral";
  detail: string;
  plan?: BlastWarmupPlan;
  warm?: WarmupDbInfo;
  canWarm: boolean;
  canRelease: boolean;
  primaryAction: "warm" | "rewarm" | "release" | "none";
  blockedReason?: string;
}

export function summariseWarmupCapacity(nodes: K8sNodeMetrics[]): WarmupCapacity {
  const userNodes = nodes.filter((n) => !isSystemPool(n.pool));
  const targetNodes = userNodes.length > 0 ? userNodes : nodes;
  let memUsedKi = 0;
  let memTotalKi = 0;
  let minFreeGiB: number | null = null;
  const pressureFlags = new Set<string>();
  for (const node of targetNodes) {
    const used = node.mem_ki ?? 0;
    const total = node.mem_capacity_ki ?? 0;
    memUsedKi += used;
    memTotalKi += total;
    if (total > 0) {
      const freeGiB = Math.max(0, total - used) / 1024 / 1024;
      minFreeGiB = minFreeGiB == null ? freeGiB : Math.min(minFreeGiB, freeGiB);
    }
    const conds = node.conditions ?? {};
    for (const key of ["MemoryPressure", "DiskPressure", "PIDPressure"] as const) {
      if (conds[key] === "True") pressureFlags.add(key);
    }
  }
  const memoryPct =
    memTotalKi > 0 ? Math.round((memUsedKi / memTotalKi) * 1000) / 10 : null;
  return {
    nodes: targetNodes.length,
    memoryPct,
    minFreeGiB,
    memoryPressure:
      pressureFlags.has("MemoryPressure") || (memoryPct != null && memoryPct >= 85),
    pressureFlags: [...pressureFlags],
  };
}

export function buildWarmupRows({
  databases,
  warmupDbs,
  planByName,
  capacity,
}: {
  databases: BlastDatabase[];
  warmupDbs: WarmupDbInfo[];
  planByName: Map<string, BlastWarmupPlan>;
  capacity: WarmupCapacity;
}): WarmupRow[] {
  const names = new Set<string>();
  for (const db of databases) names.add(db.name);
  for (const db of warmupDbs) names.add(db.name);
  for (const candidate of WARMUP_CANDIDATES) names.add(candidate.value);

  return [...names]
    .map((name) => {
      const db = databases.find((item) => item.name === name);
      const warm = warmupDbs.find((item) => item.name === name);
      const candidate = WARMUP_CANDIDATES.find((item) => item.value === name);
      const plan = planByName.get(name);
      const downloaded = Boolean(db);
      const sharded = Boolean(db?.sharded && (db.shard_sets?.length ?? 0) > 0);
      const warmReady = warm?.status === "Ready";
      const warming = warm?.status === "Loading";
      const failed = warm?.status === "Failed";
      const partial =
        warm != null &&
        !warmReady &&
        !warming &&
        !failed &&
        warm.nodes_ready > 0 &&
        warm.nodes_ready < warm.total_jobs;
      const blocked =
        plan != null && plan.feasible === false && plan.status !== "no_db_size";
      const pressure = capacity.memoryPressure && (warmReady || warming);

      let cacheLabel = "Not warm";
      let cacheTone: WarmupRow["cacheTone"] = "neutral";
      if (pressure) {
        cacheLabel = `Memory pressure · ${warm?.nodes_ready ?? 0}/${warm?.total_jobs ?? "?"}`;
        cacheTone = "pressure";
      } else if (warmReady) {
        cacheLabel = `Warm · ${warm.nodes_ready}/${warm.total_jobs} nodes`;
        cacheTone = "ready";
      } else if (warming) {
        cacheLabel = `${shortWarmupPhase(warm)} · ${warm.nodes_ready}/${warm.total_jobs} nodes`;
        cacheTone = "loading";
      } else if (partial) {
        cacheLabel = `Partial · ${warm!.nodes_ready}/${warm!.total_jobs} nodes`;
        cacheTone = "pressure";
      } else if (failed) {
        cacheLabel = `Failed · ${warm!.nodes_failed}/${warm!.total_jobs} nodes`;
        cacheTone = "blocked";
      } else if (blocked) {
        cacheLabel = "Blocked";
        cacheTone = "blocked";
      }

      const canWarm = downloaded && !warming && !blocked && !warmReady;
      const canRelease = Boolean(warm);
      const primaryAction: WarmupRow["primaryAction"] = warmReady
        ? "release"
        : warm
          ? "rewarm"
          : canWarm
            ? "warm"
            : "none";
      const shardLabel = sharded
        ? `${db!.shard_sets!.join("/")}-way shards`
        : db?.sharding_in_progress
          ? "Sharding"
          : "Not sharded";
      const detail = plan
        ? `Needs about ${plan.per_node_gib.toFixed(1)} GiB per node; safe budget ${plan.safe_node_budget_gib.toFixed(1)} GiB.`
        : downloaded
          ? "Warmup fit has not been estimated for this cluster yet."
          : "Download the database before warming it on AKS nodes.";
      const warmupProgress = warm ? formatWarmupProgress(warm) : undefined;

      return {
        name,
        label: candidate?.label ?? name,
        sizeLabel: db?.total_bytes
          ? formatBytes(db.total_bytes)
          : (candidate?.size ?? "—"),
        storageLabel: downloaded ? "Downloaded" : "Not downloaded",
        shardLabel,
        cacheLabel,
        cacheTone,
        detail: warmupProgress ?? detail,
        plan,
        warm,
        canWarm,
        canRelease,
        primaryAction,
        blockedReason: blocked
          ? plan!.message
          : downloaded
            ? undefined
            : "Database is not downloaded.",
      };
    })
    .filter((row) => row.storageLabel === "Downloaded" || row.warm != null)
    .sort((a, b) => a.name.localeCompare(b.name));
}

export function formatWarmupProgress(warm: WarmupDbInfo): string | undefined {
  if (warm.status !== "Loading" && warm.status !== "Partial") return undefined;
  const phase = warm.active_phase_label ?? shortWarmupPhase(warm);
  const message = warm.active_message ? ` · ${warm.active_message}` : "";
  const progress = `${warm.nodes_ready}/${warm.total_jobs} nodes ready`;
  const active = warm.nodes_active > 0 ? ` · ${warm.nodes_active} active` : "";
  const elapsed = formatDuration(warm.elapsed_seconds);
  const remaining = formatDuration(warm.estimated_remaining_seconds);
  const phaseCounts = formatPhaseCounts(warm.phase_counts);
  if (remaining) {
    return `${phase}${message} · ${progress}${active} · ${elapsed ?? "just started"} elapsed · about ${remaining} remaining${phaseCounts ? ` · ${phaseCounts}` : ""}.`;
  }
  if (elapsed) {
    return `${phase}${message} · ${progress}${active} · ${elapsed} elapsed · ETA after the first shard finishes${phaseCounts ? ` · ${phaseCounts}` : ""}.`;
  }
  return `${phase}${message} · ${progress}${active} · ETA after the first shard finishes${phaseCounts ? ` · ${phaseCounts}` : ""}.`;
}

export function shortWarmupPhase(warm: WarmupDbInfo): string {
  switch (warm.active_phase) {
    case "copying_files":
      return "Copying";
    case "touching_memory":
      return "RAM warmup";
    case "verifying_db":
      return "Verifying";
    case "waiting":
      return "Waiting";
    case "starting":
      return "Starting";
    case "failed":
      return "Failed";
    case "completed":
      return "Warm";
    default:
      return "Warming";
  }
}

export function formatPhaseCounts(counts?: Record<string, number>): string {
  if (!counts) return "";
  const labels: Record<string, string> = {
    copying_files: "copying",
    touching_memory: "RAM",
    verifying_db: "verifying",
    waiting: "waiting",
    starting: "starting",
    completed: "done",
    failed: "failed",
    unknown: "running",
  };
  return Object.entries(counts)
    .filter(([, count]) => count > 0)
    .map(([phase, count]) => `${labels[phase] ?? phase} ${count}`)
    .join(" · ");
}

export function formatDuration(seconds?: number): string | undefined {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return undefined;
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))} sec`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder > 0 ? `${hours} hr ${remainder} min` : `${hours} hr`;
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let value = bytes;
  let idx = 0;
  while (value >= 1024 && idx < units.length - 1) {
    value /= 1024;
    idx += 1;
  }
  return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)} ${units[idx]}`;
}
