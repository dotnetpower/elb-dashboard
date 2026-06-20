import { useQuery } from "@tanstack/react-query";
import { Ban, CheckCircle2, Circle, Clock, GitBranch } from "lucide-react";

import { blastApi } from "@/api/blast";
import type { BlastMessageTrace } from "@/api/blast.types";

import {
  STAGE_LABELS,
  fmtTraceMs,
  stageDisplayState,
  traceTerminallyFailed,
  visibleTraceStages,
} from "./messageTraceModel";

/**
 * MessageTraceCard — renders the Service Bus message lifecycle for a BLAST job
 * (enqueued → received → row_created → routed → submitted → running →
 * succeeded|failed → completion_published) plus the derived dwell/latency
 * metrics, so an operator can see where a message is and how long each hop took.
 *
 * SRP: display + its own history-bearing fetch. No business logic, no polling
 * beyond a calm refetch while the job is still in flight. The trace is only
 * present on the job detail when fetched with `history: true`, so this card owns
 * that query rather than widening the page's main (history-less) poll.
 */

function fmtClock(ts: string): string {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleTimeString();
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <span
      style={{
        display: "inline-flex",
        flexDirection: "column",
        gap: 2,
        padding: "6px 10px",
        borderRadius: 6,
        background: "var(--glass-bg-strong)",
        minWidth: 96,
      }}
    >
      <span className="muted" style={{ fontSize: 11 }}>
        {label}
      </span>
      <strong style={{ fontVariantNumeric: "tabular-nums", fontSize: 13 }}>{value}</strong>
    </span>
  );
}

export function MessageTraceCard({ jobId, isActive }: { jobId: string; isActive: boolean }) {
  const query = useQuery({
    queryKey: ["blast-job-trace", jobId],
    queryFn: () => blastApi.getJob(jobId, { history: true, includeDatabaseMetadata: false }),
    enabled: Boolean(jobId),
    // Refetch while in flight so new stages surface; calm once terminal.
    refetchInterval: isActive ? 8_000 : false,
    staleTime: 5_000,
  });

  const trace: BlastMessageTrace | undefined = query.data?.message_trace;
  // The card is only meaningful for Service-Bus / OpenAPI-plane jobs that carry
  // a trace. A dashboard-Celery job has no message lifecycle, so render nothing
  // rather than an empty shell.
  if (!trace || trace.stages.length === 0) return null;

  const reached = new Set(trace.stages.map((s) => s.stage));
  const tsByStage = new Map(trace.stages.map((s) => [s.stage, s.ts]));
  const visible = visibleTraceStages(trace);
  const terminalFailed = traceTerminallyFailed(reached);

  return (
    <section className="glass-card" style={{ padding: "14px 16px" }}>
      <h3
        style={{
          margin: "0 0 10px 0",
          fontSize: 14,
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        <GitBranch size={15} strokeWidth={1.5} /> Message lifecycle
      </h3>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 12 }}>
        <Metric label="Queue dwell" value={fmtTraceMs(trace.metrics.queue_dwell_ms)} />
        <Metric label="Submit latency" value={fmtTraceMs(trace.metrics.submit_latency_ms)} />
        <Metric label="End-to-end" value={fmtTraceMs(trace.metrics.e2e_ms)} />
      </div>

      <ol style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 6 }}>
        {visible.map((stage) => {
          const ts = tsByStage.get(stage);
          const display = stageDisplayState(stage, reached, terminalFailed);
          // done = blue check + timestamp; failed = red dot + timestamp;
          // canceled = grey ban + "canceled" (a success-path stage mooted by a
          // terminal failure, incl. "Result delivered"); pending = grey clock.
          const rowOpacity =
            display === "done" || display === "failed"
              ? 1
              : display === "canceled"
                ? 0.6
                : 0.45;
          return (
            <li
              key={stage}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                fontSize: 13,
                opacity: rowOpacity,
              }}
            >
              {display === "failed" ? (
                <Circle size={14} strokeWidth={1.5} color="var(--danger, #d9777a)" />
              ) : display === "done" ? (
                <CheckCircle2 size={14} strokeWidth={1.5} color="var(--accent)" />
              ) : display === "canceled" ? (
                <Ban size={14} strokeWidth={1.5} className="muted" />
              ) : (
                <Clock size={14} strokeWidth={1.5} className="muted" />
              )}
              <span style={{ flex: 1 }}>{STAGE_LABELS[stage] ?? stage}</span>
              <span
                className="muted"
                style={{ fontVariantNumeric: "tabular-nums", fontSize: 12 }}
              >
                {display === "canceled"
                  ? "canceled"
                  : (display === "done" || display === "failed") && ts
                    ? fmtClock(ts)
                    : "pending"}
              </span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
