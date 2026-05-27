/**
 * JobLine — one row inside `<JobsSection>`. Pure presentation: receives
 * the already-classified `JobRowView` plus a parent-supplied `nowMs`
 * tick so we render at most one timer for the whole jobs list (instead
 * of one per row).
 *
 * Layout is a tabular row with explicit columns:
 * `PROGRAM | DATABASE | JOB TITLE | (USER) | STATUS | AGE | DURATION`
 * so the AKS card preview reads like a proper table — column headers in
 * `<JobsTableHeader>` carry the (age)/(duration) labels so individual
 * cells stay compact.
 */

import { useNavigate } from "react-router-dom";
import { AlertTriangle, Loader2 } from "lucide-react";

import { isActiveJobState } from "@/components/cards/ClusterBento/jobMapping";
import type {
  DisplayJobState,
  JobRowView,
} from "@/components/cards/ClusterBento/jobTypes";
import { statusColor } from "@/constants";
import { timeAgo } from "@/pages/BlastJobs/dateGroup";

import { ownerLabel, summariseNote } from "./helpers";

export const JOB_ROW_GRID_GAP = 10;

// Column widths: PROGRAM | DATABASE | JOB TITLE | (USER) | STATUS | AGE | DURATION
const JOB_ROW_GRID_WITH_USER =
  "64px 140px minmax(0, 1fr) 72px 92px 64px 76px";
const JOB_ROW_GRID_WITHOUT_USER =
  "64px 140px minmax(0, 1fr) 92px 64px 76px";

export function jobRowGridTemplate(showUser: boolean): string {
  return showUser ? JOB_ROW_GRID_WITH_USER : JOB_ROW_GRID_WITHOUT_USER;
}

interface Props {
  job: JobRowView;
  ownerUpn?: string | null;
  /** Parent-owned "now" — re-rendered once a second for active jobs. */
  nowMs: number;
  /** When false, the User column is collapsed because no job in this
   *  roster has an owner. Lets the row reclaim space for the title. */
  showUser: boolean;
}

export function JobLine({ job, ownerUpn, nowMs, showUser }: Props) {
  const navigate = useNavigate();
  const phaseColor = statusColor(job.state.toLowerCase());
  const elapsedSec = computeElapsedSec(job, nowMs);
  const submitter = describeSubmitter(ownerUpn);
  const noteText = summariseNote(job.note);
  const active = isActiveJobState(job.state);
  const isFailed = job.state === "Failed";
  const noteTone = noteSeverity(noteText, isFailed);

  const createdAt = job.createdAt;
  const timeAgoLabel = createdAt ? stripAgoSuffix(timeAgo(createdAt)) : "—";
  const durationLabel = formatDuration(elapsedSec);
  const durationCaption = active ? "elapsed" : "duration";

  const goToJob = () => navigate(`/blast/jobs/${encodeURIComponent(job.jobId)}`);
  const fullHoverText = job.note
    ? `${job.state} · ${job.jobId} · ${job.note}`
    : `${job.state} · ${job.jobId}`;

  const titleText = job.title || job.jobId;
  const gridTemplate = jobRowGridTemplate(showUser);
  const timingAria = createdAt
    ? `${timeAgoLabel} old, ${durationCaption} ${durationLabel}`
    : "timing unknown";

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
      className={active ? "pulse-job-row pulse-job-row--active" : "pulse-job-row"}
      style={{
        display: "grid",
        gridTemplateColumns: gridTemplate,
        alignItems: "center",
        gap: JOB_ROW_GRID_GAP,
        padding: "5px 8px",
        borderRadius: 6,
        background: "var(--pulse-row-bg)",
        border: "1px solid var(--border-weak)",
        borderLeft: `3px solid ${phaseColor}`,
        cursor: "pointer",
      }}
      title={fullHoverText}
      aria-label={`Open job ${titleText}, ${job.state}, ${timingAria}.`}
    >
      <span
        className="pulse-job-program"
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          minWidth: 0,
          color: "var(--accent)",
          fontWeight: 600,
          fontSize: 11.5,
          fontFamily:
            "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        <span
          className="pulse-job-bullet"
          style={{
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            width: 12,
            height: 14,
            flexShrink: 0,
          }}
        >
          {active ? (
            <Loader2
              size={11}
              className="spin"
              color={phaseColor}
              strokeWidth={2.5}
              aria-hidden="true"
            />
          ) : (
            <span
              aria-hidden="true"
              style={{
                width: 7,
                height: 7,
                borderRadius: 999,
                background: phaseColor,
              }}
            />
          )}
        </span>
        <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
          {job.program}
        </span>
      </span>
      <span
        className="pulse-job-db"
        title={job.db}
        style={{
          color: "var(--text-primary)",
          fontWeight: 500,
          fontSize: 11.5,
          fontFamily:
            "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
          minWidth: 0,
        }}
      >
        {job.db}
      </span>
      <span
        className="pulse-job-title"
        title={titleText}
        style={{
          color: "var(--text-primary)",
          fontWeight: 600,
          fontSize: 11.5,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          minWidth: 0,
        }}
      >
        {titleText}
      </span>
      {showUser && (
        <span
          title={submitter.title}
          className="pulse-job-user"
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
      )}
      <span className="pulse-job-status-cell" style={{ textAlign: "center" }}>
        <span
          className="pulse-job-status-pill"
          style={{
            display: "inline-block",
            fontSize: 9,
            textTransform: "uppercase",
            letterSpacing: "0.04em",
            padding: "2px 7px",
            borderRadius: 999,
            background: `color-mix(in srgb, ${phaseColor} 75%, transparent)`,
            color: "#0b1220",
            fontWeight: 700,
            whiteSpace: "nowrap",
            boxShadow: `0 0 0 1px color-mix(in srgb, ${phaseColor} 35%, transparent)`,
          }}
        >
          {job.state}
        </span>
      </span>
      <span
        className="pulse-job-timeago"
        title={createdAt ? new Date(createdAt).toLocaleString() : ""}
        style={{
          fontSize: 10,
          color: "var(--text-muted)",
          whiteSpace: "nowrap",
          textAlign: "right",
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {timeAgoLabel}
      </span>
      <span
        className="pulse-job-duration"
        title={createdAt ? `${durationCaption}: ${durationLabel}` : ""}
        style={{
          fontSize: 10,
          color: "var(--text-muted)",
          whiteSpace: "nowrap",
          textAlign: "right",
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {createdAt ? durationLabel : "—"}
      </span>
      {noteText && (
        <div
          className={`pulse-job-stripe pulse-job-stripe--${noteTone}`}
          style={{
            gridColumn: "1 / -1",
            display: "flex",
            alignItems: "center",
            gap: 6,
            marginTop: 4,
            padding: "3px 7px",
            borderRadius: 4,
            fontSize: 10,
            fontWeight: 500,
            background:
              noteTone === "danger"
                ? "color-mix(in srgb, var(--danger) 14%, transparent)"
                : noteTone === "warning"
                  ? "color-mix(in srgb, var(--warning) 14%, transparent)"
                  : "var(--glass-bg-strong)",
            color:
              noteTone === "danger"
                ? "var(--danger)"
                : noteTone === "warning"
                  ? "var(--warning)"
                  : "var(--text-muted)",
          }}
        >
          <AlertTriangle size={11} strokeWidth={2} aria-hidden="true" />
          <span
            style={{
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              flex: 1,
              minWidth: 0,
            }}
          >
            {noteText}
          </span>
        </div>
      )}
    </div>
  );
}

function stripAgoSuffix(label: string): string {
  return label.endsWith(" ago") ? label.slice(0, -4) : label;
}

function noteSeverity(
  note: string | null | undefined,
  isFailed: boolean,
): "danger" | "warning" | "info" {
  if (!note) return "info";
  if (isFailed) return "danger";
  const low = note.toLowerCase();
  if (
    low.includes("oomkilled") ||
    low.includes("unschedulable") ||
    low.includes("error") ||
    low.includes("failed")
  )
    return "danger";
  if (
    low.startsWith("slow") ||
    low.startsWith("stalled") ||
    low.includes("warn")
  )
    return "warning";
  return "info";
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
