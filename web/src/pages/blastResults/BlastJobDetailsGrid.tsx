import { CheckCircle2, Clock, Copy } from "lucide-react";

import { ElapsedTimer } from "@/components/BlastFilePreview";
import type { BlastJobSummary } from "@/api/endpoints";

interface BlastJobDetailsGridProps {
  job: BlastJobSummary;
  effectivePhase: string;
  effectiveColor: string;
  isRunning: boolean;
  copiedId: boolean;
  onCopyJobId: () => void;
}

/**
 * Two-column metadata grid: Job ID, program, db, status, timing, and the
 * config snapshot / infrastructure rows when the job has them.
 *
 * The Copy-to-clipboard interaction is owned by the parent (so the toast +
 * timeout state lives next to other actions) and threaded in via props.
 */
export function BlastJobDetailsGrid({
  job,
  effectivePhase,
  effectiveColor,
  isRunning,
  copiedId,
  onCopyJobId,
}: BlastJobDetailsGridProps) {
  const config = job.config_snapshot as Record<string, unknown> | undefined;
  const infra = job.infrastructure as Record<string, unknown> | undefined;

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "140px 1fr",
        gap: "var(--space-2) var(--space-4)",
        fontSize: 13,
      }}
    >
      <span className="muted">Job ID</span>
      <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <code className="code-val">{job.job_id}</code>
        <button
          className={`copy-btn${copiedId ? " copy-btn--copied" : ""}`}
          onClick={onCopyJobId}
          title="Copy Job ID"
        >
          {copiedId ? <CheckCircle2 size={12} /> : <Copy size={12} />}
        </button>
      </span>
      <span className="muted">Program</span>
      <span>{job.program}</span>
      <span className="muted">Database</span>
      <span style={{ wordBreak: "break-all" }}>{job.db}</span>
      <span className="muted">Status</span>
      <span style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
        <span
          style={{
            width: 8,
            height: 8,
            borderRadius: 999,
            background: effectiveColor,
            boxShadow: `0 0 8px ${effectiveColor}`,
          }}
        />
        {effectivePhase === "submit_failed" ? "failed" : effectivePhase}
      </span>
      <span className="muted">Created</span>
      <span>{job.created_at ? new Date(job.created_at).toLocaleString() : "—"}</span>
      <span className="muted">Duration</span>
      <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <Clock size={12} strokeWidth={1.5} style={{ color: "var(--text-faint)" }} />
        {job.created_at && isRunning ? (
          <ElapsedTimer startTime={job.created_at} />
        ) : job.created_at && job.updated_at ? (
          formatDuration(
            new Date(job.updated_at as string).getTime() -
              new Date(job.created_at).getTime(),
          )
        ) : (
          "—"
        )}
      </span>
      {config && (
        <>
          <span className="muted">E-value</span>
          <span>{String(config.evalue ?? "—")}</span>
          <span className="muted">Max targets</span>
          <span>{String(config.max_target_seqs ?? "—")}</span>
          <span className="muted">Machine</span>
          <span>{String(config.machine_type ?? "—")}</span>
          <span className="muted">Nodes</span>
          <span>{String(config.num_nodes ?? "—")}</span>
        </>
      )}
      {infra && (
        <>
          <span className="muted">Cluster</span>
          <span>
            <code style={{ fontSize: 11 }}>{String(infra.cluster_name ?? "—")}</code>
          </span>
          <span className="muted">Region</span>
          <span>{String(infra.region ?? "—")}</span>
          <span className="muted">Resource Group</span>
          <span>{String(infra.resource_group ?? "—")}</span>
          <span className="muted">Storage</span>
          <span>{String(infra.storage_account ?? "—")}</span>
        </>
      )}
    </div>
  );
}

function formatDuration(ms: number): string {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  if (h > 0) return `${h}h ${m % 60}m ${s % 60}s`;
  if (m > 0) return `${m}m ${s % 60}s`;
  return `${s}s`;
}
