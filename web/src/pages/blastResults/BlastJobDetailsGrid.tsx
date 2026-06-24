import { memo, useMemo, type CSSProperties } from "react";
import { CheckCircle2, Clock, Copy } from "lucide-react";

import { ElapsedTimer } from "@/components/BlastFilePreview";
import { phaseLabel, queueReasonText } from "@/constants";
import type { BlastJobSummary } from "@/api/endpoints";
import {
  buildBlastCommandPreview,
  formatOutfmt,
  formatRunSeconds,
  isExternalJob,
  taxonomyFilterLabel,
} from "@/pages/blastResults/configFormat";

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
function BlastJobDetailsGridComponent({
  job,
  effectivePhase,
  effectiveColor,
  isRunning,
  copiedId,
  onCopyJobId,
}: BlastJobDetailsGridProps) {
  const config = job.config_snapshot as Record<string, unknown> | undefined;
  const infra = job.infrastructure as Record<string, unknown> | undefined;
  const command = buildBlastCommandPreview(job.program, job.db, config ?? null);
  // When the job is waiting in line, explain why beneath the status label so
  // the details view matches the job list's QUEUED secondary line.
  const queueReason =
    effectivePhase === "submit_failed" ? null : queueReasonText(effectivePhase);
  const gridStyle = useMemo<CSSProperties>(
    () => ({
      display: "grid",
      gridTemplateColumns: "140px 1fr",
      gap: "var(--space-2) var(--space-4)",
      fontSize: 13,
    }),
    [],
  );
  const statusDotStyle = useMemo<CSSProperties>(
    () => ({
      width: 8,
      height: 8,
      borderRadius: 999,
      background: effectiveColor,
      boxShadow: `0 0 8px ${effectiveColor}`,
    }),
    [effectiveColor],
  );

  return (
    <div style={gridStyle}>
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
        <span style={statusDotStyle} />
        <span style={{ display: "flex", flexDirection: "column" }}>
          <span>
            {effectivePhase === "submit_failed"
              ? "failed"
              : phaseLabel(effectivePhase)}
          </span>
          {queueReason && (
            <span className="muted" style={{ fontSize: 11 }}>
              {queueReason}
            </span>
          )}
        </span>
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
      {config ? (
        <>
          <span className="muted">Output format</span>
          <span style={{ wordBreak: "break-all" }}>{formatOutfmt(config)}</span>
          <span className="muted">E-value</span>
          <span>{String(config.evalue ?? "—")}</span>
          <span className="muted">Max targets</span>
          <span>{String(config.max_target_seqs ?? "—")}</span>
          {config.word_size != null && config.word_size !== "" && (
            <>
              <span className="muted">Word size</span>
              <span>{String(config.word_size)}</span>
            </>
          )}
          {config.dust != null && config.dust !== "" && (
            <>
              <span className="muted">Dust</span>
              <span>{String(config.dust)}</span>
            </>
          )}
          {taxonomyFilterLabel(config) && (
            <>
              <span className="muted">Taxonomy filter</span>
              <span>{taxonomyFilterLabel(config)}</span>
            </>
          )}
          {config.machine_type != null && config.machine_type !== "" && (
            <>
              <span className="muted">Machine</span>
              <span>{String(config.machine_type)}</span>
            </>
          )}
          {config.num_nodes != null && config.num_nodes !== "" && (
            <>
              <span className="muted">Nodes</span>
              <span>{String(config.num_nodes)}</span>
            </>
          )}
        </>
      ) : isExternalJob(job.submission_source) ? (
        <>
          <span className="muted">Parameters</span>
          <span className="muted" style={{ fontStyle: "italic" }}>
            not recorded for this job
          </span>
        </>
      ) : null}
      {(job.blast_version || job.db_version || job.run_seconds != null) && (
        <>
          {job.blast_version && (
            <>
              <span className="muted">BLAST version</span>
              <span>{String(job.blast_version)}</span>
            </>
          )}
          {job.db_version && (
            <>
              <span className="muted">DB version</span>
              <span>{String(job.db_version)}</span>
            </>
          )}
          {job.run_seconds != null && (
            <>
              <span className="muted">Run time</span>
              <span>{formatRunSeconds(job.run_seconds)}</span>
            </>
          )}
        </>
      )}
      {job.query_length != null && (
        <>
          <span className="muted">Query length</span>
          <span>{`${Number(job.query_length).toLocaleString()} ${
            job.molecule === "protein" ? "aa" : "nt"
          }`}</span>
        </>
      )}
      {job.molecule && (
        <>
          <span className="muted">Molecule</span>
          <span>{String(job.molecule)}</span>
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
      {command && (
        <div style={{ gridColumn: "1 / -1", marginTop: 4 }}>
          <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>
            BLAST command
          </div>
          <code
            style={{
              display: "block",
              fontSize: 11,
              whiteSpace: "pre-wrap",
              wordBreak: "break-all",
              padding: "6px 8px",
              borderRadius: 6,
              background: "var(--surface-2, rgba(255,255,255,0.04))",
            }}
          >
            {command}
          </code>
        </div>
      )}
      {config && (
        <details style={{ gridColumn: "1 / -1", marginTop: 2 }}>
          <summary className="muted" style={{ fontSize: 11, cursor: "pointer" }}>
            Raw parameters
          </summary>
          <pre
            style={{
              fontSize: 11,
              whiteSpace: "pre-wrap",
              wordBreak: "break-all",
              marginTop: 4,
            }}
          >
            {JSON.stringify(config, null, 2)}
          </pre>
        </details>
      )}
    </div>
  );
}

export const BlastJobDetailsGrid = memo(BlastJobDetailsGridComponent);

function formatDuration(ms: number): string {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  if (h > 0) return `${h}h ${m % 60}m ${s % 60}s`;
  if (m > 0) return `${m}m ${s % 60}s`;
  return `${s}s`;
}
