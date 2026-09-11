import { Fragment, memo, useMemo, type CSSProperties } from "react";
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
import {
  formatTimingSeconds,
  stableProcessingSeconds,
  stableQueueSeconds,
  stableTimeToResultSeconds,
} from "@/pages/blastResults/timingModel";

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
  const timeToResultSeconds = stableTimeToResultSeconds(job);
  const queueSeconds = stableQueueSeconds(job);
  const processingSeconds = stableProcessingSeconds(job);
  const submitSeconds = job.timing?.submit_seconds ?? null;
  const deliverySeconds = job.timing?.status_delivery_seconds ?? null;
  const unattributedSeconds = job.timing?.unattributed_seconds ?? null;
  const timingPhases = job.timing?.phases;
  const gridStyle = useMemo<CSSProperties>(
    () => ({
      display: "grid",
      gridTemplateColumns: "minmax(108px, 140px) minmax(0, 1fr)",
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
    <div style={gridStyle} data-testid="blast-run-details-grid">
      <span className="muted">Job ID</span>
      <span style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
        <code className="code-val" style={{ overflowWrap: "anywhere" }}>
          {job.job_id}
        </code>
        <button
          className={`copy-btn${copiedId ? " copy-btn--copied" : ""}`}
          onClick={onCopyJobId}
          title="Copy Job ID"
          aria-label="Copy Job ID"
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
            {effectivePhase === "submit_failed" ? "failed" : phaseLabel(effectivePhase)}
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
      <span className="muted">{isRunning ? "Elapsed" : "Time to result"}</span>
      <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <Clock size={12} strokeWidth={1.5} style={{ color: "var(--text-faint)" }} />
        {job.created_at && isRunning ? (
          <ElapsedTimer startTime={job.created_at} />
        ) : timeToResultSeconds !== null ? (
          formatTimingSeconds(timeToResultSeconds)
        ) : (
          "—"
        )}
      </span>
      {queueSeconds !== null && (
        <>
          <span className="muted">
            {job.timing?.queue_complete === false ? "Execution queue" : "Queue wait"}
          </span>
          <span>{formatTimingSeconds(queueSeconds)}</span>
        </>
      )}
      {submitSeconds != null && (
        <>
          <span className="muted">Submit</span>
          <span>{formatTimingSeconds(submitSeconds)}</span>
        </>
      )}
      {processingSeconds !== null && (
        <>
          <span className="muted">Processing</span>
          <span>{formatTimingSeconds(processingSeconds)}</span>
        </>
      )}
      {unattributedSeconds != null && unattributedSeconds > 0 && (
        <>
          <span className="muted">Other execution</span>
          <span>{formatTimingSeconds(unattributedSeconds)}</span>
        </>
      )}
      {deliverySeconds != null && (
        <>
          <span className="muted">Status delivery</span>
          <span>{formatTimingSeconds(deliverySeconds)}</span>
        </>
      )}
      {timingPhases &&
        (
          [
            ["Orchestration", timingPhases.orchestration_seconds],
            ["Kubernetes setup", timingPhases.k8s_setup_seconds],
            ["BLAST containers", timingPhases.blast_seconds],
            ["Result export", timingPhases.export_seconds],
            ["Finalizer", timingPhases.finalizer_seconds],
          ] as const
        ).map(([label, seconds]) =>
          seconds == null ? null : (
            <Fragment key={label}>
              <span className="muted">{label}</span>
              <span>{formatTimingSeconds(seconds)}</span>
            </Fragment>
          ),
        )}
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
          {job.run_seconds != null && processingSeconds === null && (
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
