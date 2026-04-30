import { useParams, useSearchParams, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Download, ArrowLeft, RefreshCw } from "lucide-react";

import { blastApi, type BlastResultFile } from "@/api/endpoints";
import { statusColor } from "@/constants";

export function BlastResults() {
  const { jobId } = useParams<{ jobId: string }>();
  const [searchParams] = useSearchParams();
  const subscriptionId = searchParams.get("subscription_id") ?? "";
  const storageAccount = searchParams.get("storage_account") ?? "";

  const jobQuery = useQuery({
    queryKey: ["blast-job", jobId],
    queryFn: () => blastApi.getJob(jobId!),
    enabled: Boolean(jobId),
    refetchInterval: (q) => {
      const d = q.state.data;
      if (!d) return 5_000;
      if (d.status === "completed" || d.status === "failed") return false;
      return 10_000;
    },
  });

  const resultsQuery = useQuery({
    queryKey: ["blast-results", jobId, subscriptionId, storageAccount],
    queryFn: () => blastApi.listResults(jobId!, subscriptionId, storageAccount),
    enabled: Boolean(jobId && subscriptionId && storageAccount),
    refetchInterval: 30_000,
  });

  const job = jobQuery.data;
  const files = resultsQuery.data?.files ?? [];
  const phase = job?.phase || job?.status || "unknown";
  const color = statusColor(phase);

  const handleDownload = async (file: BlastResultFile) => {
    if (!jobId) return;
    const resp = await blastApi.downloadResult(
      jobId,
      subscriptionId,
      storageAccount,
      file.name,
    );
    window.open(resp.download_url, "_blank");
  };

  return (
    <div className="page-stack">
      <header>
        <Link
          to="/blast/jobs"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "var(--space-2)",
            fontSize: 13,
            marginBottom: "var(--space-3)",
          }}
        >
          <ArrowLeft size={14} strokeWidth={1.5} /> All jobs
        </Link>
        <h1 style={{ margin: 0 }}>{job?.job_title || jobId}</h1>
      </header>

      {/* Job Info */}
      <section className="glass-card glass-card--strong">
        <h3 style={{ marginTop: 0 }}>Job Details</h3>
        {!job && <div className="muted">Loading...</div>}
        {job && (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "140px 1fr",
              gap: "var(--space-2) var(--space-4)",
              fontSize: 13,
            }}
          >
            <span className="muted">Job ID</span>
            <code>{job.job_id}</code>
            <span className="muted">Program</span>
            <span>{job.program}</span>
            <span className="muted">Database</span>
            <span>{job.db}</span>
            <span className="muted">Status</span>
            <span style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
              <span
                style={{
                  width: 8,
                  height: 8,
                  borderRadius: 999,
                  background: color,
                  boxShadow: `0 0 8px ${color}`,
                }}
              />
              {phase}
            </span>
            <span className="muted">Created</span>
            <span>{job.created_at ? new Date(job.created_at).toLocaleString() : "—"}</span>
            {job.config_snapshot && (
              <>
                <span className="muted">E-value</span>
                <span>{String((job.config_snapshot as Record<string, unknown>).evalue ?? "—")}</span>
                <span className="muted">Max targets</span>
                <span>{String((job.config_snapshot as Record<string, unknown>).max_target_seqs ?? "—")}</span>
                <span className="muted">Machine</span>
                <span>{String((job.config_snapshot as Record<string, unknown>).machine_type ?? "—")}</span>
                <span className="muted">Nodes</span>
                <span>{String((job.config_snapshot as Record<string, unknown>).num_nodes ?? "—")}</span>
              </>
            )}
          </div>
        )}

        {job?.custom_status != null && (
          <pre
            className="glass-card"
            style={{
              padding: "var(--space-3)",
              fontSize: 12,
              overflow: "auto",
              marginTop: "var(--space-4)",
            }}
          >
            {JSON.stringify(job.custom_status, null, 2)}
          </pre>
        )}

        {job?.error && (
          <div
            style={{
              marginTop: "var(--space-4)",
              padding: "var(--space-3)",
              background: "rgba(224, 123, 138, 0.12)",
              borderRadius: 8,
              color: "var(--danger)",
              fontSize: 13,
            }}
          >
            {job.error}
          </div>
        )}
      </section>

      {/* Results */}
      <section className="glass-card">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <h3 style={{ margin: 0 }}>Results</h3>
          <button
            className="glass-button"
            onClick={() => resultsQuery.refetch()}
            disabled={resultsQuery.isFetching}
          >
            <RefreshCw size={14} strokeWidth={1.5} /> Refresh
          </button>
        </div>

        {!subscriptionId || !storageAccount ? (
          <p className="muted" style={{ marginTop: "var(--space-3)" }}>
            Add <code>subscription_id</code> and <code>storage_account</code> as URL
            query parameters to view results.
          </p>
        ) : files.length === 0 ? (
          <p className="muted" style={{ marginTop: "var(--space-3)" }}>
            {phase === "completed"
              ? "No result files found."
              : "Results will appear here once the job completes."}
          </p>
        ) : (
          <div style={{ marginTop: "var(--space-3)" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ borderBottom: "1px solid var(--glass-border)" }}>
                  <th style={{ textAlign: "left", padding: "var(--space-2)", color: "var(--text-muted)", fontWeight: 500, fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em" }}>
                    File
                  </th>
                  <th style={{ textAlign: "right", padding: "var(--space-2)", color: "var(--text-muted)", fontWeight: 500, fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em" }}>
                    Size
                  </th>
                  <th style={{ textAlign: "right", padding: "var(--space-2)", color: "var(--text-muted)", fontWeight: 500, fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em" }}>
                    Modified
                  </th>
                  <th style={{ width: 60 }} />
                </tr>
              </thead>
              <tbody>
                {files.map((f) => (
                  <tr key={f.name} style={{ borderBottom: "1px solid var(--glass-border)" }}>
                    <td style={{ padding: "var(--space-2)" }}>
                      <code style={{ fontSize: 12 }}>{f.name.split("/").pop()}</code>
                    </td>
                    <td style={{ padding: "var(--space-2)", textAlign: "right" }} className="muted">
                      {f.size != null ? formatBytes(f.size) : "—"}
                    </td>
                    <td style={{ padding: "var(--space-2)", textAlign: "right" }} className="muted">
                      {f.last_modified ? new Date(f.last_modified).toLocaleString() : "—"}
                    </td>
                    <td style={{ padding: "var(--space-2)", textAlign: "right" }}>
                      <button
                        className="glass-button"
                        onClick={() => handleDownload(f)}
                        title="Download"
                      >
                        <Download size={14} strokeWidth={1.5} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}
