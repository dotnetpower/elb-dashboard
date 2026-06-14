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
import {
  blastDbBlockedReason,
  blastDbReadinessLabel,
  blastDbReadinessTone,
  getBlastDbReadiness,
  type BlastDbReadinessTone,
} from "@/utils/blastDbReady";

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
  /** Tone hint for the storage status pill (replaces hard-coded label match). */
  storageTone: BlastDbReadinessTone;
  /** True when the underlying storage DB is genuinely ready for warmup. */
  storageReady: boolean;
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
      // Authoritative readiness — `Boolean(db)` is NOT enough because
      // prepare-db writes the metadata blob (and thus surfaces the DB in
      // /api/blast/databases) the moment a copy starts. Only
      // `copy_status.phase === "completed"` (or legacy `file_count > 0`)
      // means the on-disk volumes are usable.
      const readiness = getBlastDbReadiness(db ?? undefined);
      const storageReady = readiness.ready;
      // An already-warm DB on AKS is implicitly Storage-ready (warmup could
      // not have started without complete files). This keeps the row
      // green for legacy DBs whose metadata blob is missing entirely.
      const effectiveStorageReady = storageReady || warm != null;
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

      let cacheLabel = "AKS cache not warm";
      let cacheTone: WarmupRow["cacheTone"] = "neutral";
      if (pressure) {
        cacheLabel = `AKS cache pressure · ${warm?.nodes_ready ?? 0}/${warm?.total_jobs ?? "?"}`;
        cacheTone = "pressure";
      } else if (warmReady) {
        cacheLabel = `AKS cache ready · ${warm.nodes_ready}/${warm.total_jobs}`;
        cacheTone = "ready";
      } else if (warming) {
        cacheLabel = `AKS cache ${shortWarmupPhase(warm).toLowerCase()} · ${warm.nodes_ready}/${warm.total_jobs}`;
        cacheTone = "loading";
      } else if (partial) {
        cacheLabel = `AKS cache partial · ${warm!.nodes_ready}/${warm!.total_jobs}`;
        cacheTone = "pressure";
      } else if (failed) {
        cacheLabel = `AKS cache failed · ${warm!.nodes_failed}/${warm!.total_jobs}`;
        cacheTone = "blocked";
      } else if (blocked) {
        cacheLabel = "AKS cache blocked";
        cacheTone = "blocked";
      }

      // Warm action requires a genuinely ready DB — never let the user fire
      // a warmup task against an in-flight copy (the warmup task would auto-
      // shard / vmtouch incomplete volumes and surface as a confusing
      // failure several minutes later).
      const canWarm = storageReady && !warming && !blocked && !warmReady;
      const canRelease = Boolean(warm);
      const primaryAction: WarmupRow["primaryAction"] = warmReady
        ? "release"
        : warm
          ? "rewarm"
          : canWarm
            ? "warm"
            : "none";
      const shardLabel = sharded
        ? `Shard layouts · ${db!.shard_sets!.join("/")}`
        : db?.sharding_in_progress
          ? "Shard layouts building"
          : warm
            ? `AKS cache shards · ${warm.shards?.length || warm.total_jobs}`
            : "Shard layouts unknown";
      const inFlightBlockedReason = !storageReady && db ? blastDbBlockedReason(readiness) : null;
      const detail = plan
        ? `Needs about ${plan.per_node_gib.toFixed(1)} GiB per node; safe budget ${plan.safe_node_budget_gib.toFixed(1)} GiB.`
        : inFlightBlockedReason
          ? inFlightBlockedReason
          : storageReady
            ? "Warmup fit has not been estimated for this cluster yet."
            : warm
              ? "AKS warmup is running; Storage catalogue details are not available in this panel yet."
              : "Prepare this database in Storage before warming it on AKS nodes.";
      const warmupProgress = warm ? formatWarmupProgress(warm) : undefined;

      // Storage-side pill: use readiness verdict for downloaded DBs, fall
      // back to legacy ready/not-ready for the warm-only / completely
      // unknown rows. We never show "Storage DB ready" for an in-flight DB.
      const storageLabel = db
        ? blastDbReadinessLabel(readiness)
        : effectiveStorageReady
          ? "Storage DB ready"
          : "Storage DB not ready";
      const storageTone: BlastDbReadinessTone = db
        ? blastDbReadinessTone(readiness)
        : effectiveStorageReady
          ? "ok"
          : "neutral";
      return {
        name,
        label: candidate?.label ?? name,
        sizeLabel: db?.total_bytes
          ? formatBytes(db.total_bytes)
          : (candidate?.size ?? "—"),
        storageLabel,
        storageTone,
        storageReady: effectiveStorageReady,
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
          : inFlightBlockedReason
            ? inFlightBlockedReason
            : effectiveStorageReady
              ? undefined
              : "Storage DB is not ready.",
      };
    })
    // Keep rows visible when (a) the DB is genuinely ready, (b) it's mid-copy
    // / mid-update (so the user sees progress instead of the row vanishing),
    // or (c) an AKS warmup record exists. Pure catalogue entries that have
    // never been touched stay hidden as before.
    .filter((row) => row.storageReady || row.warm != null || row.storageTone !== "neutral")
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

/**
 * Decide whether the warmup progress bar should render in an indeterminate
 * (animated) state instead of a determinate fill.
 *
 * A warmup run is genuinely active the whole time `status === "Loading"`, but
 * the determinate percent only exists once a pod's azcopy emits a `"%"` log
 * line. During the bootstrap window (image start, azcopy login, the first
 * seconds of a fast small-DB copy) and any later gap where no pod reports a
 * percent, `pct` collapses to 0 — which previously painted a frozen empty bar
 * and made a working warmup look stuck. In that window we show an honest
 * indeterminate bar ("active, progress unknown") rather than fabricating an
 * advancing number. The bar becomes determinate the instant `pct` is a real
 * positive value.
 */
export function isWarmupProgressIndeterminate(
  warm: Pick<WarmupDbInfo, "status">,
  pct: number,
): boolean {
  return warm.status === "Loading" && (!Number.isFinite(pct) || pct <= 0);
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
