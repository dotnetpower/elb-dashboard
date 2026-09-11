import type { BlastJobSummary } from "@/api/endpoints";

import { formatTimingSeconds } from "../blastResults/timingModel";
import { recentTimingStats } from "./timingStats";

function Stat({
  label,
  p50,
  p95,
}: {
  label: string;
  p50: number | null;
  p95: number | null;
}) {
  return (
    <span style={{ display: "inline-flex", alignItems: "baseline", gap: 5 }}>
      <span className="muted">{label}</span>
      <strong style={{ fontVariantNumeric: "tabular-nums" }}>
        p50 {formatTimingSeconds(p50)} · p95 {formatTimingSeconds(p95)}
      </strong>
    </span>
  );
}

export function RecentTimingStrip({ jobs }: { jobs: BlastJobSummary[] }) {
  const stats = recentTimingStats(jobs);
  if (stats.processingSamples === 0 && stats.completeQueueSamples === 0) return null;

  return (
    <section
      aria-label="Recent BLAST timing"
      style={{
        display: "flex",
        alignItems: "center",
        flexWrap: "wrap",
        gap: "8px 18px",
        padding: "8px 0",
        borderTop: "1px solid var(--border-weak)",
        borderBottom: "1px solid var(--border-weak)",
        fontSize: 11,
      }}
    >
      <span style={{ fontWeight: 600 }}>Recent timing</span>
      {stats.completeTimeToResultSamples > 0 && (
        <Stat
          label="Time to result"
          p50={stats.timeToResultP50}
          p95={stats.timeToResultP95}
        />
      )}
      {stats.completeQueueSamples > 0 && (
        <Stat label="Queue" p50={stats.queueP50} p95={stats.queueP95} />
      )}
      {stats.processingSamples > 0 && (
        <Stat label="Processing" p50={stats.processingP50} p95={stats.processingP95} />
      )}
      <span
        className="muted"
        title={
          stats.queuePressure === "high"
            ? "Recent queue p95 exceeds processing p50. Check workload-node saturation before increasing the admission cap."
            : "Queue pressure requires at least five paired, complete timing samples in the currently loaded recent jobs."
        }
      >
        {stats.queuePressure === "high"
          ? "Queue pressure high"
          : stats.queuePressure === "normal"
            ? "Queue pressure normal"
            : "Queue pressure unknown"}
        {` · ${stats.completeQueueSamples}/${stats.totalJobs} complete queue samples · ${stats.pressureSamples} paired`}
      </span>
    </section>
  );
}
