import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { History, ArrowRight, AlertTriangle } from "lucide-react";

import { blastApi, type BlastJobForAccession } from "@/api/blast";
import { statusColor, phaseLabel } from "@/constants";
import { timeAgo } from "@/pages/BlastJobs/dateGroup";

const MAX_ROWS = 10;

function StatusPill({ status, phase }: { status: string; phase: string }) {
  const label = phaseLabel((phase || status || "").toLowerCase());
  const color = statusColor((phase || status || "").toLowerCase());
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        fontSize: 11,
        fontWeight: 500,
        color: "var(--text)",
        whiteSpace: "nowrap",
      }}
    >
      <span
        aria-hidden
        style={{
          width: 7,
          height: 7,
          borderRadius: "50%",
          background: color,
          flexShrink: 0,
        }}
      />
      {label || "—"}
    </span>
  );
}

function rangeLabel(job: BlastJobForAccession): string {
  if (job.seq_start != null && job.seq_stop != null) {
    return `${job.seq_start.toLocaleString()}..${job.seq_stop.toLocaleString()}`;
  }
  return "whole sequence";
}

/**
 * "Your BLAST jobs for this accession" — the one card on Sequence Detail that
 * NCBI can never show, because it joins the public record to the caller's own
 * past runs. Read-only, owner-scoped on the server, and additive: a jobstate
 * outage degrades to a calm line and never blocks the record view.
 *
 * Coverage: only jobs submitted through the accession path match. A run where
 * the user pasted FASTA copied from this record will not appear — the empty
 * copy is worded to avoid implying "you never searched this sequence".
 */
export function JobBackReferenceCard({ accession }: { accession: string }) {
  const query = useQuery({
    queryKey: ["jobs-for-accession", accession, "base"],
    queryFn: () => blastApi.getJobsForAccession(accession, { match: "base", limit: MAX_ROWS }),
    enabled: accession.length > 0,
    // This is the caller's own history, not live infra — a modest stale window
    // is plenty and avoids the aggressive polling the Jobs page uses.
    staleTime: 30_000,
  });

  const data = query.data;
  const jobs = data?.jobs ?? [];
  const degraded = data?.degraded ?? false;

  return (
    <div
      className="glass-card glass-card--strong"
      style={{ padding: 16, display: "grid", gap: 12 }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 8,
          flexWrap: "wrap",
        }}
      >
        <h2 style={{ margin: 0, fontSize: 14, display: "flex", alignItems: "center", gap: 8 }}>
          <History size={15} strokeWidth={1.5} />
          Your BLAST jobs for this accession
        </h2>
        {jobs.length > 0 && (
          <Link
            to="/blast/jobs"
            className="glass-button glass-button--ghost"
            style={{
              fontSize: 12,
              padding: "2px 8px",
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
            }}
          >
            View all in BLAST jobs
            <ArrowRight size={12} strokeWidth={1.5} />
          </Link>
        )}
      </div>

      {query.isLoading && (
        <div className="muted" style={{ fontSize: 12 }}>
          Looking up your past runs…
        </div>
      )}

      {!query.isLoading && degraded && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            color: "var(--warning)",
            fontSize: 12,
          }}
        >
          <AlertTriangle size={13} strokeWidth={1.5} />
          <span>Could not load your job history right now. The record above is unaffected.</span>
        </div>
      )}

      {!query.isLoading && !degraded && jobs.length === 0 && (
        <div className="muted" style={{ fontSize: 12, lineHeight: 1.5 }}>
          No accession-mode BLAST job found for this accession yet. Use{" "}
          <strong style={{ fontWeight: 600, color: "var(--text)" }}>Use in BLAST</strong> above to
          run one.
        </div>
      )}

      {!query.isLoading && !degraded && jobs.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table
            style={{
              width: "100%",
              borderCollapse: "collapse",
              fontSize: 12,
            }}
          >
            <thead>
              <tr style={{ color: "var(--text-muted)", textAlign: "left" }}>
                <th style={{ padding: "4px 8px 6px 0", fontWeight: 500 }}>Status</th>
                <th style={{ padding: "4px 8px 6px 0", fontWeight: 500 }}>Database</th>
                <th style={{ padding: "4px 8px 6px 0", fontWeight: 500 }}>Range</th>
                <th style={{ padding: "4px 8px 6px 0", fontWeight: 500 }}>Submitted</th>
                <th style={{ padding: "4px 0 6px 0", fontWeight: 500, textAlign: "right" }} />
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.job_id} style={{ borderTop: "1px solid var(--border, rgba(255,255,255,0.06))" }}>
                  <td style={{ padding: "8px 8px 8px 0" }}>
                    <StatusPill status={job.status} phase={job.phase} />
                  </td>
                  <td
                    style={{
                      padding: "8px 8px 8px 0",
                      fontFamily: "var(--font-mono, monospace)",
                      color: "var(--text)",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {job.database || "—"}
                  </td>
                  <td style={{ padding: "8px 8px 8px 0", color: "var(--text)", whiteSpace: "nowrap" }}>
                    {rangeLabel(job)}
                  </td>
                  <td
                    className="muted"
                    style={{ padding: "8px 8px 8px 0", whiteSpace: "nowrap" }}
                    title={job.created_at ? new Date(job.created_at).toLocaleString() : ""}
                  >
                    {job.created_at ? timeAgo(job.created_at) : "—"}
                  </td>
                  <td style={{ padding: "8px 0", textAlign: "right" }}>
                    <Link
                      to={job.detail_url}
                      className="glass-button glass-button--ghost"
                      style={{
                        fontSize: 11,
                        padding: "2px 8px",
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 4,
                      }}
                      title="Open job detail"
                    >
                      Open
                      <ArrowRight size={12} strokeWidth={1.5} />
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
