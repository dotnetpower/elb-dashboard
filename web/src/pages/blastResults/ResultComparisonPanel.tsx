import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { GitCompareArrows, Loader2 } from "lucide-react";

import { blastApi } from "@/api/blast";
import type {
  BlastHitComparison,
  BlastJobSummary,
  BlastResultComparison,
} from "@/api/endpoints";
import { formatApiError } from "@/api/client";

interface ResultComparisonPanelProps {
  job?: BlastJobSummary | null;
}

export function comparisonSummaryText(summary: BlastResultComparison["summary"]): string {
  return `${summary.added} added · ${summary.removed} removed · ${summary.changed} changed`;
}

function isCompleted(job: BlastJobSummary): boolean {
  return ["completed", "succeeded", "success"].includes(job.status.toLowerCase());
}

function formatMetric(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  if (value !== 0 && Math.abs(value) < 0.001) return value.toExponential(2);
  return value.toLocaleString(undefined, { maximumFractionDigits: 3 });
}

function metricDelta(item: BlastHitComparison): string {
  if (item.status !== "changed") return "—";
  const before = item.before?.max_bitscore;
  const after = item.after?.max_bitscore;
  if (before == null || after == null) return "bitscore changed";
  const delta = after - before;
  return `${delta >= 0 ? "+" : ""}${formatMetric(delta)} bitscore`;
}

export function ResultComparisonPanel({ job }: ResultComparisonPanelProps) {
  const [againstJobId, setAgainstJobId] = useState("");
  const jobsQuery = useQuery({
    queryKey: ["blast-comparison-candidates", job?.job_id],
    queryFn: () => blastApi.listJobs({ limit: 100 }),
    enabled: Boolean(job),
    staleTime: 30_000,
  });
  const candidates = useMemo(
    () =>
      (jobsQuery.data?.jobs ?? []).filter(
        (candidate) => candidate.job_id !== job?.job_id && isCompleted(candidate),
      ),
    [job?.job_id, jobsQuery.data?.jobs],
  );
  const mutation = useMutation({
    mutationFn: (target: string) => blastApi.compareJobs(job!.job_id, target),
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const target = againstJobId.trim();
    if (!job || !target || mutation.isPending) return;
    mutation.mutate(target);
  };

  if (!job || !isCompleted(job)) {
    return (
      <section className="glass-card glass-card--strong">
        <h3 style={{ marginTop: 0 }}>Comparison</h3>
        <p className="muted" style={{ marginBottom: 0 }}>
          Comparison becomes available after this search completes.
        </p>
      </section>
    );
  }

  const result = mutation.data;
  return (
    <section className="glass-card glass-card--strong" aria-labelledby="comparison-title">
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <GitCompareArrows size={16} strokeWidth={1.5} />
        <h3 id="comparison-title" style={{ margin: 0 }}>
          Result comparison
        </h3>
      </div>
      <form
        onSubmit={submit}
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 1fr) auto",
          gap: "var(--space-2)",
          marginTop: "var(--space-3)",
        }}
      >
        <div>
          <label htmlFor="comparison-job-id" className="muted" style={{ fontSize: 11 }}>
            Compare against completed search
          </label>
          <input
            id="comparison-job-id"
            className="glass-input"
            list="comparison-job-options"
            value={againstJobId}
            onChange={(event) => {
              setAgainstJobId(event.target.value);
              mutation.reset();
            }}
            placeholder="Job ID"
            autoComplete="off"
            style={{ width: "100%", marginTop: 4 }}
          />
          <datalist id="comparison-job-options">
            {candidates.map((candidate) => (
              <option key={candidate.job_id} value={candidate.job_id}>
                {candidate.job_title || candidate.db}
              </option>
            ))}
          </datalist>
        </div>
        <button
          className="glass-button glass-button--primary"
          type="submit"
          disabled={!againstJobId.trim() || mutation.isPending}
          style={{ alignSelf: "end", minWidth: 112 }}
        >
          {mutation.isPending ? <Loader2 size={14} className="spin" /> : <GitCompareArrows size={14} />}
          {mutation.isPending ? "Comparing…" : "Compare"}
        </button>
      </form>

      {mutation.isError && (
        <div role="alert" style={{ color: "var(--danger)", marginTop: 12, fontSize: 12 }}>
          {formatApiError(mutation.error, "comparison")}
        </div>
      )}

      {result && (
        <>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))",
              gap: "var(--space-3)",
              marginTop: "var(--space-4)",
            }}
          >
            <Metric label="Summary" value={comparisonSummaryText(result.summary)} />
            <Metric label="Common" value={result.summary.common} />
            <Metric label="Unchanged" value={result.summary.unchanged} />
            <Metric label="Jaccard" value={`${formatMetric(result.summary.jaccard_percent)}%`} />
          </div>
          {(result.partial || result.truncated) && (
            <div
              role="status"
              style={{ color: "var(--warning)", marginTop: 12, fontSize: 12 }}
            >
              This comparison is partial because an input or response limit was reached.
            </div>
          )}
          <div className="muted" style={{ marginTop: 12, fontSize: 11 }}>
            Added and removed hits describe this search relative to {result.against_job_id}.
          </div>
          <div style={{ overflowX: "auto", marginTop: "var(--space-4)" }}>
            <table className="data-table" style={{ width: "100%", minWidth: 760 }}>
              <thead>
                <tr>
                  <th>Change</th>
                  <th>Query</th>
                  <th>Subject</th>
                  <th>Before score</th>
                  <th>After score</th>
                  <th>Delta</th>
                </tr>
              </thead>
              <tbody>
                {result.items.map((item) => (
                  <tr key={`${item.status}:${item.query_id}:${item.subject_id}`}>
                    <td style={{ color: changeTone(item.status), fontWeight: 650 }}>
                      {item.status}
                    </td>
                    <td>{item.query_id}</td>
                    <td title={item.title || undefined} style={{ maxWidth: 300 }}>
                      <code className="code-val">{item.subject_id}</code>
                    </td>
                    <td>{formatMetric(item.before?.max_bitscore)}</td>
                    <td>{formatMetric(item.after?.max_bitscore)}</td>
                    <td>{metricDelta(item)}</td>
                  </tr>
                ))}
                {result.items.length === 0 && (
                  <tr>
                    <td colSpan={6} className="muted">
                      No added, removed, or changed hits were found.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

function changeTone(status: BlastHitComparison["status"]): string {
  if (status === "added") return "var(--success)";
  if (status === "removed") return "var(--danger)";
  return "var(--warning)";
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div style={{ minWidth: 0 }}>
      <div className="muted" style={{ fontSize: 11 }}>
        {label}
      </div>
      <div style={{ fontWeight: 650, overflowWrap: "anywhere" }}>{value}</div>
    </div>
  );
}