/**
 * JobLine — one row inside `<JobsSection>`. Pure presentation: receives
 * the already-classified `JobRowView` plus a parent-supplied `nowMs`
 * tick so we render at most one timer for the whole jobs list (instead
 * of one per row).
 *
 * The row is keyboard-activatable (role="link"): pressing Enter or
 * clicking navigates to the per-job results page so users can drill in
 * without hunting for a separate "Open" button.
 */

import { User } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { isActiveJobState } from "@/components/cards/ClusterBento/jobMapping";
import type { DisplayJobState, JobRowView } from "@/components/cards/ClusterBento/atoms";

import { DbChip, JobStatePill } from "./atoms";
import {
  estimateEtaSec,
  jobStateTone,
  jobTimeText,
  noteToneFor,
  ownerLabel,
  prettifyQueryLabel,
  summariseNote,
} from "./helpers";

interface Props {
  job: JobRowView;
  ownerUpn?: string | null;
  /** Parent-owned "now" — re-rendered once a second for active jobs. */
  nowMs: number;
}

export function JobLine({ job, ownerUpn, nowMs }: Props) {
  const navigate = useNavigate();
  const tone = jobStateTone(job.state);
  const splitsTotal = job.splitsTotal ?? 0;
  const splitsDone = job.splitsDone ?? 0;
  const pct = splitsTotal === 0 ? 0 : Math.min(1, splitsDone / splitsTotal);
  const progressWidth = splitsTotal === 0 ? 0 : Math.max(2, pct * 100);
  const elapsedSec = computeElapsedSec(job, nowMs);
  const etaSec =
    job.etaSec ??
    (job.state === "Running"
      ? estimateEtaSec({ elapsedSec, splitsDone, splitsTotal })
      : null);
  const submitter = describeSubmitter(ownerUpn);
  const noteText = summariseNote(job.note);
  const noteTone = noteToneFor(job.note);
  const queryLabel = prettifyQueryLabel(job.query);
  const borderLeftColor = stateBorderColor(job.state);

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
        gridTemplateColumns: "84px minmax(0, 1fr) 110px 96px 120px",
        alignItems: "center",
        gap: 10,
        padding: "6px 8px",
        borderRadius: 6,
        background: "var(--pulse-row-bg)",
        border: "1px solid var(--border-weak)",
        borderLeft: `3px solid ${borderLeftColor}`,
        cursor: "pointer",
      }}
      title={fullHoverText}
      aria-label={`Open job ${job.displayId} (${job.state}). ${queryLabel}.`}
    >
      <JobStatePill state={job.state} />
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: 2,
          minWidth: 0,
        }}
      >
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            minWidth: 0,
          }}
        >
          <span
            style={{
              fontSize: 9,
              fontFamily: "var(--font-mono)",
              color: "var(--text-faint)",
              border: "1px solid var(--border-weak)",
              borderRadius: 3,
              padding: "0 4px",
              flexShrink: 0,
            }}
            title={`Job id: ${job.jobId}`}
          >
            #{job.displayId}
          </span>
          <span
            style={{
              fontSize: 12,
              color: "var(--text-primary)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              fontFamily: "var(--font-mono)",
              minWidth: 0,
            }}
          >
            {queryLabel}
          </span>
        </span>
        {noteText && (
          <span
            style={{
              fontSize: 10,
              color: noteTone,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {noteText}
          </span>
        )}
      </div>
      <DbChip name={job.db} />
      <div
        style={{ display: "flex", flexDirection: "column", gap: 3 }}
        title={`${splitsDone}/${splitsTotal} splits`}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            fontSize: 10,
            color: "var(--text-faint)",
            fontVariantNumeric: "tabular-nums",
          }}
        >
          <span>
            {splitsDone}/{splitsTotal || "?"}
          </span>
          <span>{splitsTotal === 0 ? "—" : `${Math.round(pct * 100)}%`}</span>
        </div>
        <div
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={splitsTotal || 100}
          aria-valuenow={splitsTotal === 0 ? 0 : splitsDone}
          aria-label={`Splits ${splitsDone} of ${splitsTotal || "unknown"}`}
          style={{
            height: 4,
            borderRadius: 2,
            background: "var(--bg-canvas)",
            overflow: "hidden",
          }}
        >
          <div
            style={{
              width: `${progressWidth}%`,
              height: "100%",
              background: tone,
              transition: "width 200ms ease-out",
            }}
          />
        </div>
      </div>
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "flex-end",
          gap: 2,
          fontVariantNumeric: "tabular-nums",
        }}
      >
        <span style={{ fontSize: 11, color: "var(--text-primary)" }}>
          {jobTimeText(job.state, elapsedSec, etaSec)}
        </span>
        <span
          style={{
            fontSize: 10,
            color: "var(--text-faint)",
            display: "inline-flex",
            alignItems: "center",
            gap: 3,
            maxWidth: "100%",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
          title={submitter.title}
        >
          <User size={9} aria-hidden="true" />
          {submitter.label}
        </span>
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

function describeSubmitter(upn: string | null | undefined): {
  label: string;
  title: string;
} {
  const local = ownerLabel(upn);
  if (local) {
    return { label: local, title: upn ?? local };
  }
  // No UPN on the row — don't fabricate "user" since the previous
  // string made every job look like it had the same submitter.
  return { label: "—", title: "Submitter not recorded" };
}

function stateBorderColor(state: DisplayJobState): string {
  switch (state) {
    case "Failed":
      return "var(--danger)";
    case "Completed":
      return "var(--success)";
    case "Running":
      return "var(--accent)";
    case "Reducing":
      return "var(--teal)";
    case "Pending":
      return "var(--border-weak)";
    case "Unknown":
      return "var(--warning)";
  }
}

export { JobLine as default };
