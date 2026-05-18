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
import type { JobRowView } from "@/components/cards/ClusterBento/atoms";

import { JobLine, jobHasLiveTick } from "./JobLine";

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
  /** Map of job_id → full BlastJobSummary so we can read `owner_upn`
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
  jobIndex,
  clusterName,
}: Props) {
  const navigate = useNavigate();
  const anyActive = jobs.some((j) => jobHasLiveTick(j.state));
  const nowMs = useTickWhenActive(anyActive);

  const goToJobsPage = () =>
    navigate(`/blast/jobs?cluster=${encodeURIComponent(clusterName)}`);

  return (
    <div
      style={{
        padding: "10px 14px 12px 14px",
        borderTop: "1px solid var(--border-weak)",
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span
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
        </span>
        <span
          style={{
            fontSize: 10,
            color: "var(--text-faint)",
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {jobsDegraded
            ? "job state store unavailable"
            : `${activeCount} active · ${completedToday} done in 24h`}
          {!jobsDegraded && unknownCount > 0 && (
            <>
              {" · "}
              <span
                style={{ color: "var(--warning)" }}
                title="Jobs whose phase/status the dashboard could not classify"
              >
                {unknownCount} unknown
              </span>
            </>
          )}
          {!jobsDegraded && failed15m > 0 && (
            <>
              {" · "}
              <span style={{ color: "var(--danger)" }}>
                {failed15m} failed / 15m
              </span>
            </>
          )}
        </span>
      </div>

      {jobsDegraded ? (
        <div
          style={{ fontSize: 11, color: "var(--text-faint)", padding: "4px 0" }}
        >
          Counts and roster will return automatically once the job-state store
          recovers.
        </div>
      ) : jobs.length === 0 ? (
        <div
          style={{ fontSize: 11, color: "var(--text-faint)", padding: "4px 0" }}
        >
          No jobs yet —{" "}
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
          .
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {jobs.map((j) => (
            <JobLine
              key={j.jobId}
              job={j}
              ownerUpn={jobIndex.get(j.jobId)?.owner_upn}
              nowMs={nowMs}
            />
          ))}
          {moreCount > 0 && (
            <button
              type="button"
              onClick={goToJobsPage}
              title={`Open the full Jobs page filtered to ${clusterName}`}
              style={{
                marginTop: 2,
                alignSelf: "flex-start",
                background: "transparent",
                border: "none",
                color: "var(--accent)",
                fontSize: 11,
                fontWeight: 500,
                cursor: "pointer",
                padding: "2px 0",
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
              }}
            >
              +{moreCount} more job{moreCount === 1 ? "" : "s"}
              <ChevronRight size={11} aria-hidden="true" />
            </button>
          )}
        </div>
      )}
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
