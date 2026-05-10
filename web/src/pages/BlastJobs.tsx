import { useState, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Trash2, RefreshCw, AlertTriangle } from "lucide-react";

import { blastApi } from "@/api/endpoints";
import { statusColor } from "@/constants";
import { ConfirmDialog } from "@/components/ConfirmDialog";

function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ${mins % 60}m ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export function BlastJobs() {
  const queryClient = useQueryClient();
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "running" | "completed" | "failed">("all");

  const jobsQuery = useQuery({
    queryKey: ["blast-jobs"],
    queryFn: () => blastApi.listJobs(),
    refetchInterval: 20_000,
  });

  const deleteMutation = useMutation({
    mutationFn: (jobId: string) => blastApi.deleteJob(jobId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["blast-jobs"] });
    },
  });

  const allJobs = jobsQuery.data?.jobs ?? [];
  const FAILED_PHASES = ["failed", "submit_failed", "error"];
  const TERMINAL_PHASES = ["completed", ...FAILED_PHASES, "deleted"];

  const jobs = useMemo(() => {
    const sorted = [...allJobs].reverse();
    if (filter === "all") return sorted;
    return sorted.filter((j) => {
      const phase = j.phase || j.status;
      if (filter === "running") return !TERMINAL_PHASES.includes(phase);
      if (filter === "failed") return FAILED_PHASES.includes(phase);
      return phase === filter;
    });
  }, [allJobs, filter]);

  const counts = useMemo(() => {
    const c = { running: 0, completed: 0, failed: 0 };
    allJobs.forEach((j) => {
      const p = j.phase || j.status;
      if (p === "completed") c.completed++;
      else if (FAILED_PHASES.includes(p)) c.failed++;
      else if (p !== "deleted") c.running++;
    });
    return c;
  }, [allJobs]);

  return (
    <div className="page-stack">
      <header style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div className="page-header" style={{ marginBottom: 0 }}>
          <div className="page-header__title">BLAST Jobs</div>
          <div className="page-header__desc">
            {allJobs.length} total · {counts.running} running · {counts.completed} completed · {counts.failed} failed
          </div>
          {/* #21: Visual status bar */}
          {allJobs.length > 0 && (
            <div className="prog" style={{ width: 200, height: 6, marginTop: 6 }}>
              <div style={{ display: "flex", height: "100%" }}>
                <div style={{ width: `${(counts.completed / allJobs.length) * 100}%`, background: "var(--success)", borderRadius: "2px 0 0 2px" }} />
                <div style={{ width: `${(counts.running / allJobs.length) * 100}%`, background: "var(--warning)" }} />
                <div style={{ width: `${(counts.failed / allJobs.length) * 100}%`, background: "var(--danger)", borderRadius: "0 2px 2px 0" }} />
              </div>
            </div>
          )}
        </div>
        <div style={{ display: "flex", gap: "var(--space-3)" }}>
          <Link to="/blast/submit" className="glass-button glass-button--primary" style={{ textDecoration: "none" }}>
            New search
          </Link>
          <button
            className="glass-button"
            onClick={() => jobsQuery.refetch()}
            disabled={jobsQuery.isFetching}
          >
            <RefreshCw size={14} strokeWidth={1.5} /> Refresh
          </button>
        </div>
      </header>

      {/* #20: Loading skeleton */}
      {jobsQuery.isLoading && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {[1, 2, 3].map((i) => (
            <div key={i} className="skeleton skeleton-line" style={{ height: 48, width: "100%" }} />
          ))}
        </div>
      )}

      {/* Filter bar */}
      {allJobs.length > 0 && (
        <div style={{ display: "flex", gap: "var(--space-2)" }}>
          {(["all", "running", "completed", "failed"] as const).map((f) => (
            <button
              key={f}
              className={`glass-button ${filter === f ? "glass-button--primary" : ""}`}
              onClick={() => setFilter(f)}
              style={{ fontSize: 11, textTransform: "capitalize" }}
            >
              {f} {f !== "all" && `(${counts[f]})`}
            </button>
          ))}
        </div>
      )}

      {/* Delete error */}
      {deleteMutation.isError && (
        <div style={{ padding: "8px 12px", background: "rgba(224,123,138,0.08)", border: "1px solid rgba(224,123,138,0.2)", borderRadius: 6, fontSize: 12, color: "var(--danger)" }}>
          <AlertTriangle size={12} style={{ verticalAlign: "middle", marginRight: 4 }} />
          Delete failed: {(deleteMutation.error as Error).message}
        </div>
      )}

      {jobs.length === 0 && allJobs.length === 0 && (
        <section className="glass-card" style={{ textAlign: "center", padding: "var(--space-7)" }}>
          <p className="muted">No BLAST jobs yet.</p>
          <Link to="/blast/submit" className="glass-button glass-button--primary" style={{ marginTop: "var(--space-4)", textDecoration: "none" }}>
            Submit your first search
          </Link>
        </section>
      )}
      {jobs.length === 0 && allJobs.length > 0 && (
        <section className="glass-card" style={{ textAlign: "center", padding: "var(--space-5)" }}>
          <p className="muted">No {filter} jobs.</p>
        </section>
      )}

      {jobs.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
          {jobs.map((job) => {
            const phase = job.phase || job.status;
            const color = statusColor(phase);
            return (
              <section
                key={job.job_id}
                className="glass-card"
                style={{ padding: "var(--space-4)" }}
              >
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "1fr auto",
                    gap: "var(--space-3)",
                    alignItems: "center",
                  }}
                >
                  <div>
                    <div style={{ display: "flex", alignItems: "center", gap: "var(--space-3)" }}>
                      <span
                        style={{
                          width: 8,
                          height: 8,
                          borderRadius: 999,
                          background: color,
                          boxShadow: `0 0 8px ${color}`,
                          flexShrink: 0,
                        }}
                      />
                      <Link
                        to={`/blast/jobs/${encodeURIComponent(job.job_id)}`}
                        style={{ textDecoration: "none", color: "inherit", fontWeight: 600 }}
                      >
                        {job.job_title || job.job_id}
                      </Link>
                      <span
                        className="glass-badge muted"
                      >
                        {phase}
                      </span>
                    </div>
                    <div className="muted" style={{ fontSize: 12, marginTop: "var(--space-1)" }}>
                      {job.program} · {job.db.split("/").pop()} · {job.created_at ? timeAgo(job.created_at) : "—"}
                    </div>
                  </div>
                  <div style={{ display: "flex", gap: "var(--space-2)" }}>
                    <button
                      className="glass-button"
                      onClick={() => setDeleteTarget(job.job_id)}
                      disabled={deleteMutation.isPending}
                      aria-label={`Delete job ${job.job_title || job.job_id}`}
                    >
                      <Trash2 size={14} strokeWidth={1.5} />
                    </button>
                  </div>
                </div>
              </section>
            );
          })}
        </div>
      )}

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete BLAST Job"
        message="This will stop the job and clean up resources. This cannot be undone."
        confirmLabel="Delete"
        onConfirm={() => {
          if (deleteTarget) deleteMutation.mutate(deleteTarget);
          setDeleteTarget(null);
        }}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
