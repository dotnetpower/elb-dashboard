import { Database, Flame, Layers, Loader2 } from "lucide-react";
import type { UseMutationResult } from "@tanstack/react-query";

import { LegendDot } from "./LegendDot";
import type { DbChip } from "./types";

type ShardMutationResult = UseMutationResult<unknown, Error, string, unknown>;

/**
 * Per-database readiness strip — each chip explicitly names its stage
 * (downloaded / sharded / warming / ready / failed) so users can tell
 * incomplete state from in-progress at a glance.
 *
 * The header `· ready` chip is only the *workspace* (cluster init) signal;
 * this strip is the per-database breakdown that actually matters.
 */
export function DatabaseChipStrip({
  dbChips,
  infeasibleDbs,
  dbListDegraded,
  shardMutation,
  shardingDb,
  shardError,
  clusterNumNodes,
  clusterMachineType,
}: {
  dbChips: DbChip[];
  infeasibleDbs: DbChip[];
  dbListDegraded: boolean;
  shardMutation: ShardMutationResult;
  shardingDb: string | null;
  shardError: { name: string; msg: string } | null;
  clusterNumNodes: number;
  clusterMachineType: string;
}) {
  const warmChips = dbChips.filter((db) => db.warm != null);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          gap: 10,
          flexWrap: "wrap",
          paddingLeft: 2,
        }}
      >
        <span
          className="muted"
          style={{
            fontSize: 9,
            textTransform: "uppercase",
            letterSpacing: "0.06em",
          }}
        >
          Databases
        </span>
        <span
          className="muted cluster-db-legend"
          style={{
            fontSize: 10,
            display: "inline-flex",
            gap: 8,
            flexWrap: "wrap",
          }}
          title="Node-local warm cache state only. Downloaded or sharded databases are managed in the BLAST Databases modal until they are actually warming or warm."
        >
          <LegendDot color="var(--accent)" label="warming" />
          <LegendDot color="var(--success)" label="ready" />
          <LegendDot color="var(--warning)" label="failed" />
        </span>
      </div>
      {dbListDegraded && warmChips.length === 0 ? (
        <div
          className="muted"
          style={{ fontSize: 10, paddingLeft: 2 }}
          title="BLAST databases listing is unavailable from this caller. Run scripts/dev/storage-public-access.sh on for local debugging, or rely on the Storage card from inside the Container App."
        >
          · listing unavailable (storage public access disabled)
        </div>
      ) : (
        <>
          {warmChips.length > 0 ? (
            <>
              <div className="dv3-warmup-strip" style={{ marginTop: 0 }}>
                {warmChips.map((db) => (
                  <DbChipNode
                    key={db.name}
                    db={db}
                    shardMutation={shardMutation}
                    shardingDb={shardingDb}
                  />
                ))}
              </div>
              <DbChipStatusMessages dbChips={warmChips} shardingDb={shardingDb} />
            </>
          ) : (
            <div className="muted" style={{ fontSize: 10, paddingLeft: 2 }}>
              No warmed databases yet. Mark databases for Auto warm in BLAST Databases, or
              open Details to warm one now.
              {infeasibleDbs.length > 0 && clusterMachineType
                ? ` ${infeasibleDbs.length} database${infeasibleDbs.length === 1 ? "" : "s"} may need a larger cluster than ${clusterNumNodes} × ${clusterMachineType}.`
                : ""}
            </div>
          )}
        </>
      )}
      {shardError && (
        <div
          className="muted"
          style={{ fontSize: 10, paddingLeft: 2, color: "var(--warning)" }}
          title={shardError.msg}
        >
          · sharding failed for {shardError.name}: {shardError.msg}
        </div>
      )}
    </div>
  );
}

function DbChipStatusMessages({
  dbChips,
  shardingDb,
}: {
  dbChips: DbChip[];
  shardingDb: string | null;
}) {
  const messages = dbChips
    .map((db) => dbChipVisibleStatusMessage(db, shardingDb))
    .filter((message): message is string => Boolean(message));
  if (messages.length === 0) return null;
  return (
    <div
      className="muted"
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 2,
        paddingLeft: 2,
        fontSize: 10,
        lineHeight: 1.45,
      }}
    >
      {messages.map((message) => (
        <span key={message}>· {message}</span>
      ))}
    </div>
  );
}

export function dbChipVisibleStatusMessage(
  db: DbChip,
  shardingDb: string | null,
): string | null {
  const w = db.warm;
  const isReady = w?.status === "Ready";
  const isLoading = w?.status === "Loading";
  const isFailed = w?.status === "Failed";
  const isStale = db.warmStale || w?.status === "Stale";
  const isShardingNow = db.shardingInProgress || shardingDb === db.name;
  if (isShardingNow) {
    return `${db.name}: building prepared shard layouts for faster sharded submits.`;
  }
  if (isLoading && w) {
    const phase = shortWarmupPhase(w);
    const progress = `${w.nodes_ready}/${w.total_jobs} nodes ready`;
    const remaining =
      w.estimated_remaining_seconds != null
        ? `, about ${formatCompactDuration(w.estimated_remaining_seconds)} left`
        : w.elapsed_seconds != null
          ? `, running ${formatCompactDuration(w.elapsed_seconds)}`
          : "";
    const message = (w.active_message ?? w.active_phase_label ?? "").trim();
    return `${db.name}: ${phase} DB cache (${progress}${remaining})${message ? ` - ${message}` : ""}.`;
  }
  if (isStale) {
    return `${db.name}: warm cache is stale and should be refreshed before sharded throughput runs.`;
  }
  if (isFailed && w) {
    return `${db.name}: warmup failed on ${w.nodes_failed}/${w.total_jobs} nodes.`;
  }
  if (w && !isReady && w.nodes_ready > 0) {
    return `${db.name}: warm cache is only partially ready (${w.nodes_ready}/${w.total_jobs} nodes).`;
  }
  return null;
}

function DbChipNode({
  db,
  shardMutation,
  shardingDb,
}: {
  db: DbChip;
  shardMutation: ShardMutationResult;
  shardingDb: string | null;
}) {
  const w = db.warm;
  const isReady = w?.status === "Ready";
  const isLoading = w?.status === "Loading";
  const isFailed = w?.status === "Failed";
  const isStale = db.warmStale || w?.status === "Stale";
  const isPartial = Boolean(
    w && !isReady && !isLoading && !isFailed && w.nodes_ready > 0,
  );
  // Server-side in-progress flag wins over the local mutation pending
  // state — that way a page reload (or a second tab) still shows
  // "sharding…" while the daemon thread is mid-run. The optimistic
  // local signal still covers the gap between POST returning 202 and
  // the next list-databases poll completing.
  const isShardingNow = db.shardingInProgress || shardingDb === db.name;

  // Stage classification — mutually exclusive labels so the chip's text
  // always answers "what step is this DB at?".
  let stageLabel = "";
  let stageVariant = "";
  let StageIcon = Database;
  if (isShardingNow) {
    stageLabel = "sharding…";
    stageVariant = "loading";
    StageIcon = Loader2;
  } else if (isStale) {
    const count = db.warmSourceVersions.length;
    stageLabel = count > 1 ? `warm stale · ${count} versions` : "warm stale";
    stageVariant = "warn";
    StageIcon = Flame;
  } else if (isReady) {
    stageLabel = `ready · ${w!.nodes_ready}/${w!.total_jobs}`;
    stageVariant = "";
    StageIcon = Flame;
  } else if (isLoading) {
    const reason = shortWarmupReason(w!);
    stageLabel = `${shortWarmupPhase(w!)}${reason ? ` · ${reason}` : ""} · ${w!.nodes_ready}/${w!.total_jobs}`;
    if (w!.estimated_remaining_seconds != null) {
      stageLabel += ` · ~${formatCompactDuration(w!.estimated_remaining_seconds)} left`;
    } else if (w!.elapsed_seconds != null) {
      stageLabel += ` · ${formatCompactDuration(w!.elapsed_seconds)}`;
    }
    stageVariant = "loading";
    StageIcon = Loader2;
  } else if (isFailed) {
    stageLabel = `warmup failed · ${w!.nodes_failed}/${w!.total_jobs}`;
    stageVariant = "warn";
    StageIcon = Database;
  } else if (isPartial) {
    stageLabel = `partial · ${w!.nodes_ready}/${w!.total_jobs}`;
    stageVariant = "warn";
    StageIcon = Flame;
  } else if (w) {
    stageLabel = `warm status unknown · ${w.nodes_ready}/${w.total_jobs}`;
    stageVariant = "faint";
    StageIcon = Flame;
  } else if (db.shardingError) {
    stageLabel = "shard failed · click to retry";
    stageVariant = "warn";
    StageIcon = Database;
  } else if (db.sharded) {
    stageLabel = `sharded · ×${db.shardLayouts}`;
    stageVariant = "shard";
    StageIcon = Layers;
  } else {
    stageLabel = "downloaded only";
    stageVariant = "faint";
    StageIcon = Database;
  }
  // A downloaded-only chip (or a chip whose previous shard attempt
  // errored) is the actionable state. We refuse to fire while another
  // shard mutation is in flight to keep the per-cluster traffic low —
  // the lock on the server is the real guard.
  const isShardable =
    !isShardingNow &&
    (!db.sharded || !!db.shardingError) &&
    !w &&
    !shardMutation.isPending;

  const titleParts: string[] = [db.name];
  titleParts.push(stageLabel);
  if (db.sourceVersion) titleParts.push(`storage ${db.sourceVersion}`);
  if (db.warmSourceVersion) titleParts.push(`warm ${db.warmSourceVersion}`);
  if (db.warmSourceVersions.length > 1) {
    titleParts.push(`warm versions ${db.warmSourceVersions.join(", ")}`);
  }
  if (db.shardingError) {
    titleParts.push(db.shardingError);
    titleParts.push("click to retry sharding");
  } else if (db.sharded && !isReady && !isLoading && !isFailed) {
    titleParts.push(
      `prepare-db has uploaded ${db.shardLayouts} preset layout${db.shardLayouts === 1 ? "" : "s"} — ready for elastic-blast submit (no node-side cache yet)`,
    );
  } else if (isShardable) {
    titleParts.push("click to run prepare-db sharding now");
  } else if (!db.sharded && !w) {
    titleParts.push(
      "prepare-db has not uploaded shard layouts yet — elastic-blast will shard on first submit",
    );
  } else if (isReady) {
    titleParts.push("vmtouch cache hot on every node — zero cold-start");
  }
  if (w?.elapsed_seconds != null) {
    titleParts.push(`elapsed ${formatCompactDuration(w.elapsed_seconds)}`);
  }
  if (w?.estimated_remaining_seconds != null) {
    titleParts.push(
      `estimated remaining ${formatCompactDuration(w.estimated_remaining_seconds)}`,
    );
  } else if (isLoading) {
    titleParts.push(
      "estimated remaining time is not available until at least one shard finishes",
    );
  }
  if (w?.active_phase_label) {
    titleParts.push(w.active_phase_label);
  }
  if (w?.active_message) {
    titleParts.push(w.active_message);
  }
  if (w?.phase_counts) {
    const counts = formatPhaseCounts(w.phase_counts);
    if (counts) titleParts.push(counts);
  }
  if (db.warmupPlan && db.warmupPlan.status !== "ok") {
    titleParts.push(`warmup: ${db.warmupPlan.message}`);
    for (const rec of db.warmupPlan.recommendations) {
      titleParts.push(`→ ${rec}`);
    }
  }
  const baseClass = `dv3-warmup-chip${stageVariant ? " " + stageVariant : ""}`;
  const chipBody = (
    <>
      <StageIcon
        size={11}
        strokeWidth={1.75}
        className={isLoading || isShardingNow ? "spin" : undefined}
      />
      {db.name}
      <span className="stage">{stageLabel}</span>
    </>
  );
  return isShardable ? (
    <button
      type="button"
      className={baseClass}
      title={titleParts.join(" · ")}
      onClick={() => shardMutation.mutate(db.name)}
      style={{
        cursor: "pointer",
        font: "inherit",
        appearance: "none",
        filter: "brightness(1)",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.filter = "brightness(1.18)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.filter = "brightness(1)";
      }}
    >
      {chipBody}
    </button>
  ) : (
    <span className={baseClass} title={titleParts.join(" · ")}>
      {chipBody}
    </span>
  );
}

function formatCompactDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "unknown";
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder > 0 ? `${hours}h ${remainder}m` : `${hours}h`;
}

function shortWarmupPhase(w: NonNullable<DbChip["warm"]>): string {
  switch (w.active_phase) {
    case "copying_files":
      return "copying";
    case "touching_memory":
      return "RAM";
    case "verifying_db":
      return "verifying";
    case "waiting":
      return "waiting";
    case "starting":
      return "starting";
    default:
      return "warming";
  }
}

function shortWarmupReason(w: NonNullable<DbChip["warm"]>): string {
  if (w.active_phase !== "waiting" && w.active_phase !== "failed") return "";
  const message = (w.active_message ?? "").trim();
  if (!message || message === "Waiting for container") return "";
  return message.length > 32 ? `${message.slice(0, 29)}...` : message;
}

function formatPhaseCounts(counts: Record<string, number>): string {
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
