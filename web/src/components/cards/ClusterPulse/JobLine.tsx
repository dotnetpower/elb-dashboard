/**
 * JobLine — one row inside `<JobsSection>`. Pure presentation: receives
 * the already-classified `JobRowView` plus a parent-supplied `nowMs`
 * tick so we render at most one timer for the whole jobs list (instead
 * of one per row).
 *
 * Layout mirrors the Recent searches table row (JOB · USER · STATUS ·
 * TIME) so the AKS card's preview and the dedicated Jobs page read the
 * same way.
 */

import { useNavigate } from "react-router-dom";

import { BlastJobIdentity } from "@/components/cards/BlastJobIdentity";
import { isActiveJobState } from "@/components/cards/ClusterBento/jobMapping";
import type {
  DisplayJobState,
  JobRowView,
} from "@/components/cards/ClusterBento/jobTypes";
import { statusColor } from "@/constants";
import { timeAgo } from "@/pages/BlastJobs/dateGroup";

import { jobStateTone, ownerLabel, summariseNote } from "./helpers";

interface Props {
  job: JobRowView;
  ownerUpn?: string | null;
  /** Parent-owned "now" — re-rendered once a second for active jobs. */
  nowMs: number;
}

export function JobLine({ job, ownerUpn, nowMs }: Props) {
  const navigate = useNavigate();
  const tone = jobStateTone(job.state);
  const phaseColor = statusColor(job.state.toLowerCase());
  const elapsedSec = computeElapsedSec(job, nowMs);
  const submitter = describeSubmitter(ownerUpn);
  const noteText = summariseNote(job.note);
  const active = isActiveJobState(job.state);

  const createdAt = job.createdAt;
  const timeAgoLabel = createdAt ? timeAgo(createdAt) : "—";
  const durationLabel = formatDuration(elapsedSec);
  const durationCaption = active ? "Elapsed" : "Duration";

  const goToJob = () => navigate(`/blast/jobs/${encodeURIComponent(job.jobId)}`);
  const fullHoverText = job.note
    ? `${job.state} · ${job.jobId} · ${job.note}`
    : `${job.state} · ${job.jobId}`;

  return (
    <div
      role="link"
      tabIndex={0}
      onClick={goToJob}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          goToJob();
        }
      }}
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(0, 1fr) 76px 76px 92px",
        alignItems: "center",
        gap: 8,
        padding: "5px 8px",
        borderRadius: 6,
        background: "var(--pulse-row-bg)",
        border: "1px solid var(--border-weak)",
        cursor: "pointer",
      }}
      title={fullHoverText}
      aria-label={`Open job ${job.title || job.jobId} (${job.state}).`}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          minWidth: 0,
        }}
      >
        <span
          aria-hidden="true"
          style={{
            width: 7,
            height: 7,
            borderRadius: 999,
            background: phaseColor,
            flexShrink: 0,
          }}
        />
        <BlastJobIdentity
          title={job.title}
          fallbackTitle={job.jobId}
          program={job.program}
          db={job.db}
          query={job.query}
          note={noteText}
          noteTone={tone}
          compact
        />
      </div>
      <span
        title={submitter.title}
        style={{
          fontSize: 10,
          color: "var(--text-muted)",
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {submitter.label}
      </span>
      <span style={{ textAlign: "center" }}>
        <span
          style={{
            display: "inline-block",
            fontSize: 9,
            textTransform: "uppercase",
            letterSpacing: "0.03em",
            padding: "1px 5px",
            borderRadius: 4,
            background: `${phaseColor}18`,
            color: phaseColor,
            fontWeight: 600,
            whiteSpace: "nowrap",
          }}
        >
          {job.state}
        </span>
      </span>
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "flex-end",
          gap: 0,
          fontVariantNumeric: "tabular-nums",
        }}
        title={createdAt ? new Date(createdAt).toLocaleString() : ""}
      >
        <span style={{ fontSize: 10, color: "var(--text-muted)" }}>{timeAgoLabel}</span>
        {createdAt && (
          <span style={{ fontSize: 9, color: "var(--text-faint)" }}>
            {durationCaption} {durationLabel}
          </span>
        )}
      </div>
    </div>
  );
}

export function jobHasLiveTick(state: DisplayJobState): boolean {
  return isActiveJobState(state);
}

function computeElapsedSec(j: JobRowView, nowMs: number): number {
  if (j.elapsedSec != null) return j.elapsedSec;
  if (!j.createdAt) return 0;
  const start = Date.parse(j.createdAt);
  if (!Number.isFinite(start)) return 0;
  return Math.max(0, Math.floor((nowMs - start) / 1000));
}

function formatDuration(seconds: number): string {
  const safe = Math.max(0, Math.floor(seconds));
  const days = Math.floor(safe / 86_400);
  const hours = Math.floor((safe % 86_400) / 3_600);
  const minutes = Math.floor((safe % 3_600) / 60);
  const secs = safe % 60;
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

function describeSubmitter(upn: string | null | undefined): {
  label: string;
  title: string;
} {
  const local = ownerLabel(upn);
  if (local) {
    return { label: local, title: upn ?? local };
  }
  return { label: "—", title: "Submitter not recorded" };
}

export { JobLine as default };
