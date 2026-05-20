import { AlertTriangle } from "lucide-react";

const DEGRADED_REASON_LABEL: Record<string, string> = {
  all_reads_failed:
    "Every result file failed to download. RBAC, network outage, or the storage account is unreachable.",
  aggregation_failed:
    "Hits were retrieved but the analytics aggregation crashed. Try refreshing; if it persists, the data shape may be unexpected.",
  no_result_files:
    "The job is complete, but no parseable result files are available yet.",
  no_results: "The job finished but no output blobs were produced.",
  storage_unreachable:
    "Result storage is unreachable from this API process. Check local storage network access or private endpoint reachability.",
};

export interface DegradedBannerProps {
  data: {
    degraded?: boolean;
    degraded_reason?: string;
    message?: string;
    files_parsed?: number;
    total_files?: number;
    read_failures?: number;
    truncated?: boolean;
    hit_limit_reached?: boolean;
  };
  resultsPending?: boolean;
}

/**
 * Yellow/red banner shown when the aggregate or alignments query came back
 * partial (some shards unreadable, hit cap reached, etc.). Kept in its own
 * file so any tab can include it.
 */
export function DegradedBanner({ data, resultsPending = false }: DegradedBannerProps) {
  const isError = Boolean(data.degraded);
  const colour = isError ? "var(--danger)" : "var(--warning)";
  const reasonText =
    resultsPending && data.degraded_reason === "no_result_files"
      ? "The BLAST search is still running; final result files are not available yet."
      :
    (data.degraded_reason && DEGRADED_REASON_LABEL[data.degraded_reason]) ||
    data.message ||
    data.degraded_reason ||
    null;
  return (
    <div
      className="glass-card"
      style={{
        padding: 16,
        marginBottom: 20,
        borderColor: colour,
        borderWidth: 1,
        borderStyle: "solid",
      }}
    >
      <div style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
        <AlertTriangle
          size={18}
          strokeWidth={1.5}
          style={{ color: colour, marginTop: 2, flexShrink: 0 }}
        />
        <div style={{ flex: 1 }}>
          <div style={{ color: colour, fontWeight: 600, marginBottom: 4 }}>
            {isError ? "Results are degraded" : "Results are partial"}
          </div>
          {reasonText && (
            <div style={{ fontSize: 13, color: "var(--text-primary)", marginBottom: 6 }}>
              {reasonText}
            </div>
          )}
          <div className="muted" style={{ fontSize: 12 }}>
            {typeof data.files_parsed === "number" &&
              typeof data.total_files === "number" && (
                <span>
                  Successfully parsed {data.files_parsed.toLocaleString()} of{" "}
                  {data.total_files.toLocaleString()} result file
                  {data.total_files === 1 ? "" : "s"}.{" "}
                </span>
              )}
            {typeof data.read_failures === "number" && data.read_failures > 0 && (
              <span>
                {data.read_failures.toLocaleString()} read failure
                {data.read_failures === 1 ? "" : "s"}.{" "}
              </span>
            )}
            {data.truncated && (
              <span>
                Showing the first batch only — re-run with fewer query splits for full
                coverage.
              </span>
            )}
            {data.hit_limit_reached && (
              <span>
                {" "}
                Hit review stopped at the safety cap; export the raw files for full
                coverage.
              </span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
