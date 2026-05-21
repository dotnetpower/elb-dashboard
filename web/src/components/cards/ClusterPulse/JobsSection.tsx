/**
 * JobsSection — header + roster + "+N more" affordance.
 *
 * Owns the single 1-second tick used by every active JobLine, so we
 * don't spawn one `setInterval` per row.
 */

import { useEffect, useState } from "react";
import { Activity, ChevronRight } from "lucide-react";
import { useNavigate } from "react-router-dom";

import type { BlastJobSummary } from "@/api/endpoints";
import type { JobRowView } from "@/components/cards/ClusterBento/jobTypes";

import {
  JOB_ROW_GRID_GAP,
  JobLine,
  jobHasLiveTick,
  jobRowGridTemplate,
} from "./JobLine";

interface Props {
  jobs: JobRowView[];
  moreCount: number;
  activeCount: number;
  completedToday: number;
  failed15m: number;
  /** Jobs whose state the classifier could not bucket. Surfaced so the
   *  header is honest when the roster shows N rows but active=0 /
   *  completed=0. */
  unknownCount: number;
  jobsDegraded: boolean;
  /** True while the first /api/blast/jobs request for this cluster is
   *  still in flight. Used to render a skeleton roster instead of the
   *  "No jobs yet" empty state, which previously flashed before the
   *  response landed. */
  jobsLoading: boolean;
  /** Map of job_id -> full BlastJobSummary so we can read `owner_upn`
   *  without re-querying. */
  jobIndex: Map<string, BlastJobSummary>;
  /** Name of the parent cluster, used to deep-link "+N more" into the
   *  Jobs page filtered to this cluster. */
  clusterName: string;
}

export function JobsSection({
  jobs,
  moreCount,
  activeCount,
  completedToday,
  failed15m,
  unknownCount,
  jobsDegraded,
  jobsLoading,
  jobIndex,
  clusterName,
}: Props) {
  const navigate = useNavigate();
  const anyActive = jobs.some((j) => jobHasLiveTick(j.state));
  const nowMs = useTickWhenActive(anyActive);
  const showEmptyJobsInline = !jobsDegraded && !jobsLoading && jobs.length === 0;
  const hasOwners = jobs.some((j) => {
    const upn = jobIndex.get(j.jobId)?.owner_upn;
    return typeof upn === "string" && upn.trim().length > 0;
  });

  const goToJobsPage = () =>
    navigate(`/blast/jobs?cluster=${encodeURIComponent(clusterName)}`);

  return (
    <div
      style={{
        padding: "7px 10px 9px 10px",
        borderTop: "1px solid var(--border-weak)",
        display: "flex",
        flexDirection: "column",
        gap: 6,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span
          className="pulse-jobs-label"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 5,
            fontSize: 10,
            fontWeight: 600,
            color: "var(--text-faint)",
            textTransform: "uppercase",
            letterSpacing: "0.12em",
          }}
        >
          <Activity size={10} aria-hidden="true" /> Jobs
          {!jobsDegraded && !jobsLoading && jobs.length > 0 && (
            <span
              style={{
                color: "var(--text-muted)",
                fontVariantNumeric: "tabular-nums",
                letterSpacing: "normal",
              }}
            >
              · {jobs.length}
            </span>
          )}
        </span>
        <span
          className="pulse-jobs-caption"
          style={{
            fontSize: 10,
            color: "var(--text-faint)",
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {jobsDegraded
            ? "job state store unavailable"
            : jobsLoading && jobs.length === 0
              ? "loading..."
              : `${activeCount} active · ${completedToday} done in 24h`}
          {!jobsDegraded && !jobsLoading && unknownCount > 0 && (
            <>
              {" "}
              <span
                className="pulse-jobs-chip pulse-jobs-chip--warn"
                style={{ color: "var(--warning)" }}
                title="Jobs whose phase/status the dashboard could not classify"
              >
                {unknownCount} unknown
              </span>
            </>
          )}
          {!jobsDegraded && !jobsLoading && failed15m > 0 && (
            <>
              {" "}
              <span
                className="pulse-jobs-chip pulse-jobs-chip--danger"
                style={{ color: "var(--danger)" }}
              >
                {failed15m} failed / 15m
              </span>
            </>
          )}
        </span>
        {showEmptyJobsInline && (
          <span
            style={{
              marginLeft: "auto",
              fontSize: 11,
              color: "var(--text-faint)",
            }}
          >
            No jobs yet ·{" "}
            <button
              type="button"
              onClick={() => navigate("/blast/submit")}
              style={{
                background: "transparent",
                border: "none",
                padding: 0,
                color: "var(--accent)",
                fontSize: 11,
                fontWeight: 500,
                cursor: "pointer",
                textDecoration: "underline",
                textUnderlineOffset: 2,
              }}
            >
              submit one
            </button>
          </span>
        )}
      </div>

      {jobsDegraded ? (
        <div style={{ fontSize: 11, color: "var(--text-faint)", padding: "4px 0" }}>
          Counts and roster will return automatically once the job-state store recovers.
        </div>
      ) : jobsLoading && jobs.length === 0 ? (
        <JobsSkeleton />
      ) : jobs.length > 0 ? (
        <div
          className="pulse-jobs-list"
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 3,
          }}
        >
          <JobsTableHeader showUser={hasOwners} />
          {jobs.map((j) => (
            <JobLine
              key={j.jobId}
              job={j}
              ownerUpn={jobIndex.get(j.jobId)?.owner_upn}
              nowMs={nowMs}
              showUser={hasOwners}
            />
          ))}
          {moreCount > 0 && (
            <button
              type="button"
              onClick={goToJobsPage}
              className="pulse-jobs-more-btn"
              title={`Open the full Jobs page filtered to ${clusterName}`}
              style={{
                marginTop: 4,
                alignSelf: "flex-end",
                background: "rgba(122, 167, 255, 0.12)",
                border: "1px solid color-mix(in srgb, var(--accent) 58%, transparent)",
                borderRadius: 999,
                color: "var(--accent)",
                fontSize: 11,
                fontWeight: 700,
                cursor: "pointer",
                padding: "4px 9px",
                display: "inline-flex",
                alignItems: "center",
                gap: 5,
                boxShadow: "0 0 0 1px rgba(122, 167, 255, 0.08) inset",
              }}
            >
              More jobs
              <span
                style={{
                  color: "var(--text-muted)",
                  fontWeight: 600,
                  fontVariantNumeric: "tabular-nums",
                }}
              >
                +{moreCount}
              </span>
              <ChevronRight size={13} aria-hidden="true" />
            </button>
          )}
        </div>
      ) : null}
    </div>
  );
}

/** Single 1-second tick shared across all JobLines. Stops when none of
 *  the rendered jobs are in an active state so collapsed/idle clusters
 *  don't keep React busy. */
function useTickWhenActive(enabled: boolean): number {
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled) return;
    const id = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [enabled]);
  return nowMs;
}

/** Skeleton roster shown during the first /api/blast/jobs fetch so the
 *  row doesn't briefly flash the "No jobs yet" empty state. Mirrors
 *  the JobLine row geometry so the layout doesn't jump once real rows arrive. */
function JobsSkeleton() {
  return (
    <div
      role="status"
      aria-live="polite"
      aria-label="Loading jobs"
      style={{ display: "flex", flexDirection: "column", gap: 3 }}
    >
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          className="pulse-soft pulse-job-row pulse-job-skeleton"
          style={{
            display: "grid",
            gridTemplateColumns: jobRowGridTemplate(true),
            alignItems: "center",
            gap: JOB_ROW_GRID_GAP,
            padding: "5px 8px",
            borderRadius: 6,
            background: "var(--pulse-row-bg)",
            border: "1px solid var(--border-weak)",
          }}
        >
          <SkeletonBar width="82%" height={12} />
          <SkeletonBar width="60%" height={10} />
          <SkeletonBar width="70%" height={12} />
          <SkeletonBar width="75%" height={10} />
        </div>
      ))}
    </div>
  );
}

function SkeletonBar({ width, height }: { width: string | number; height: number }) {
  return (
    <span
      aria-hidden="true"
      style={{
        display: "inline-block",
        width,
        height,
        borderRadius: 3,
        background: "var(--kpi-bar-bg)",
      }}
    />
  );
}

/** Column headers for the jobs roster — mirrors the Recent searches
 *  table header so the AKS card's preview reads the same way. The
 *  `User` column is suppressed when no job in the visible roster has
 *  an owner, freeing the row width for the title + chips. */
function JobsTableHeader({ showUser }: { showUser: boolean }) {
  const gridTemplate = jobRowGridTemplate(showUser);
  return (
    <div
      className="pulse-jobs-header"
      style={{
        display: "grid",
        gridTemplateColumns: gridTemplate,
        alignItems: "center",
        gap: JOB_ROW_GRID_GAP,
        padding: "1px 8px",
        fontSize: 9,
        fontWeight: 500,
        textTransform: "uppercase",
        letterSpacing: "0.06em",
        color: "var(--text-faint)",
        borderBottom: "1px solid var(--border-weak)",
      }}
    >
      <span>Job</span>
      {showUser && <span className="pulse-job-user">User</span>}
      <span style={{ textAlign: "center" }}>Status</span>
      <span style={{ textAlign: "right" }}>Time</span>
    </div>
  );
}
