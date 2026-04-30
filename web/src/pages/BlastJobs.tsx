import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Trash2, RefreshCw, Eye } from "lucide-react";

import { blastApi } from "@/api/endpoints";
import { statusColor } from "@/constants";
import { ConfirmDialog } from "@/components/ConfirmDialog";

export function BlastJobs() {
  const queryClient = useQueryClient();
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  const jobsQuery = useQuery({
    queryKey: ["blast-jobs"],
    queryFn: () => blastApi.listJobs(),
    refetchInterval: 10_000,
  });

  const deleteMutation = useMutation({
    mutationFn: (jobId: string) => blastApi.deleteJob(jobId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["blast-jobs"] });
    },
  });

  const jobs = jobsQuery.data?.jobs ?? [];

  return (
    <div className="page-stack">
      <header style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div>
          <h1 style={{ margin: 0 }}>BLAST Jobs</h1>
          <p className="muted" style={{ marginTop: "var(--space-2)" }}>
            {jobs.length} job{jobs.length !== 1 ? "s" : ""} tracked. Auto-refreshing.
          </p>
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

      {jobs.length === 0 && (
        <section className="glass-card" style={{ textAlign: "center", padding: "var(--space-7)" }}>
          <p className="muted">No BLAST jobs yet.</p>
          <Link to="/blast/submit" className="glass-button glass-button--primary" style={{ marginTop: "var(--space-4)", textDecoration: "none" }}>
            Submit your first search
          </Link>
        </section>
      )}

      {jobs.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
          {[...jobs].reverse().map((job) => {
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
                      <strong>{job.job_title || job.job_id}</strong>
                      <span
                        className="glass-badge muted"
                      >
                        {phase}
                      </span>
                    </div>
                    <div className="muted" style={{ fontSize: 12, marginTop: "var(--space-1)" }}>
                      {job.program} · {job.db.split("/").pop()} · {job.created_at ? new Date(job.created_at).toLocaleString() : "—"}
                    </div>
                  </div>
                  <div style={{ display: "flex", gap: "var(--space-2)" }}>
                    <Link
                      to={`/blast/jobs/${encodeURIComponent(job.job_id)}`}
                      className="glass-button"
                      style={{ textDecoration: "none" }}
                    >
                      <Eye size={14} strokeWidth={1.5} /> View
                    </Link>
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
