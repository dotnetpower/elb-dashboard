import { useQuery } from "@tanstack/react-query";
import { Layers3, RefreshCw } from "lucide-react";

import { blastApi } from "@/api/blast";
import type {
  BlastJobSummary,
  BlastShardDetail,
  BlastShardDetailsResponse,
} from "@/api/endpoints";

interface ShardDetailsCardProps {
  job?: BlastJobSummary | null;
}

const ACTIVE_REFRESH_MS = 5_000;

export function shardProgressLabel(summary: BlastShardDetailsResponse["summary"]): string {
  if (summary.total === 0) return "No shard work";
  return `${summary.terminal} of ${summary.total} settled`;
}

function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.round(seconds % 60);
  return `${minutes}m ${remainder}s`;
}

function statusTone(status: string): string {
  const value = status.toLowerCase();
  if (["completed", "succeeded", "success"].includes(value)) return "var(--success)";
  if (["failed", "error"].includes(value)) return "var(--danger)";
  if (["cancelled", "canceled", "deleted"].includes(value)) return "var(--text-faint)";
  return "var(--accent)";
}

function ShardRow({ shard }: { shard: BlastShardDetail }) {
  return (
    <tr>
      <td>
        <code className="code-val">{shard.group_id || shard.job_id}</code>
      </td>
      <td>{shard.query_file || "—"}</td>
      <td>
        <span style={{ color: statusTone(shard.status), fontWeight: 600 }}>
          {shard.status}
        </span>
        {shard.phase && shard.phase !== shard.status ? (
          <div className="muted" style={{ fontSize: 11 }}>
            {shard.phase}
          </div>
        ) : null}
      </td>
      <td style={{ fontVariantNumeric: "tabular-nums" }}>
        {formatDuration(shard.duration_seconds)}
      </td>
      <td style={{ fontVariantNumeric: "tabular-nums" }}>
        {shard.effective_search_space?.toLocaleString() ?? "—"}
      </td>
      <td style={{ maxWidth: 320, overflowWrap: "anywhere" }}>
        {shard.error ? <span style={{ color: "var(--danger)" }}>{shard.error}</span> : "—"}
      </td>
    </tr>
  );
}

export function ShardDetailsCard({ job }: ShardDetailsCardProps) {
  const hasShards = (job?.split_children?.child_count ?? 0) > 0;
  const query = useQuery({
    queryKey: ["blast-shards", job?.job_id],
    queryFn: () => blastApi.getShardDetails(job!.job_id),
    enabled: hasShards,
    refetchInterval: (current) => {
      const data = current.state.data as BlastShardDetailsResponse | undefined;
      return data && data.summary.active === 0 ? false : ACTIVE_REFRESH_MS;
    },
    staleTime: 2_000,
  });

  if (!hasShards) return null;
  const summary = query.data?.summary;
  const shards = query.data?.shards ?? [];

  return (
    <section className="glass-card glass-card--strong" aria-labelledby="shard-details-title">
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "var(--space-3)",
          marginBottom: "var(--space-3)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Layers3 size={16} strokeWidth={1.5} />
          <h3 id="shard-details-title" style={{ margin: 0 }}>
            Shard details
          </h3>
        </div>
        <button
          type="button"
          className="glass-button"
          onClick={() => void query.refetch()}
          disabled={query.isFetching}
          title="Refresh shard status"
          aria-label="Refresh shard status"
          style={{ width: 32, height: 32, padding: 0 }}
        >
          <RefreshCw size={14} className={query.isFetching ? "spin" : undefined} />
        </button>
      </div>

      {query.isError ? (
        <div role="alert" style={{ color: "var(--danger)", fontSize: 12 }}>
          Shard details are temporarily unavailable.
        </div>
      ) : !summary ? (
        <div className="muted" style={{ fontSize: 12 }}>
          Loading shard details...
        </div>
      ) : (
        <>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))",
              gap: "var(--space-3)",
              marginBottom: "var(--space-3)",
            }}
          >
            <Metric label="Progress" value={shardProgressLabel(summary)} />
            <Metric label="Completed" value={summary.completed} />
            <Metric label="Active" value={summary.active} />
            <Metric label="Failed" value={summary.failed} danger={summary.failed > 0} />
          </div>
          <div
            aria-label="Shard completion progress"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={summary.progress_percent}
            style={{
              height: 5,
              borderRadius: 3,
              overflow: "hidden",
              background: "var(--surface-2)",
              marginBottom: "var(--space-4)",
            }}
          >
            <div
              style={{
                width: `${Math.min(100, Math.max(0, summary.progress_percent))}%`,
                height: "100%",
                background: summary.failed > 0 ? "var(--warning)" : "var(--accent)",
              }}
            />
          </div>
          <div style={{ overflowX: "auto" }}>
            <table className="data-table" style={{ width: "100%", minWidth: 720 }}>
              <thead>
                <tr>
                  <th>Shard</th>
                  <th>Query file</th>
                  <th>Status</th>
                  <th>Duration</th>
                  <th>Search space</th>
                  <th>Error</th>
                </tr>
              </thead>
              <tbody>
                {shards.map((shard) => (
                  <ShardRow key={shard.job_id} shard={shard} />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

function Metric({
  label,
  value,
  danger = false,
}: {
  label: string;
  value: string | number;
  danger?: boolean;
}) {
  return (
    <div style={{ minWidth: 0 }}>
      <div className="muted" style={{ fontSize: 11 }}>
        {label}
      </div>
      <div
        style={{
          fontSize: 14,
          fontWeight: 650,
          color: danger ? "var(--danger)" : "var(--text-primary)",
          overflowWrap: "anywhere",
        }}
      >
        {value}
      </div>
    </div>
  );
}