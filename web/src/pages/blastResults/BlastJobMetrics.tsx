import { Link } from "react-router-dom";
import {
  BarChart3,
  Download,
  Loader2,
} from "lucide-react";

import { formatBytes } from "@/components/BlastFilePreview";
import type { BlastExportFormat, BlastResultFile } from "@/api/endpoints";
import { BlastJobStatusIcon } from "@/pages/blastResults/BlastJobBanners";

interface BlastJobMetricsProps {
  jobId: string;
  files: BlastResultFile[];
  phase: string;
  completedButFailed: boolean;
  hasExportTargets: boolean;
  exportingFormat: BlastExportFormat | null;
  onExport: (format: BlastExportFormat) => void;
}

/**
 * The 3-block metric strip + action row that appears below the job-details
 * grid for completed jobs. Shows file count, total bytes, status icon, and
 * the analytics / CSV / JSON buttons.
 */
export function BlastJobMetrics({
  jobId,
  files,
  phase,
  completedButFailed,
  hasExportTargets,
  exportingFormat,
  onExport,
}: BlastJobMetricsProps) {
  return (
    <>
      <div className="metric-grid" style={{ marginTop: "var(--space-3)" }}>
        <div className="metric-block">
          <div className="mv">{files.length}</div>
          <div className="mu">Result files</div>
        </div>
        <div className="metric-block">
          <div className="mv">
            {formatBytes(files.reduce((sum, f) => sum + (f.size || 0), 0))}
          </div>
          <div className="mu">Total size</div>
        </div>
        <div className="metric-block">
          <div
            className="mv"
            style={{
              color: completedButFailed ? "var(--danger)" : "var(--success)",
            }}
          >
            <BlastJobStatusIcon completedButFailed={completedButFailed} />
            {completedButFailed ? "failed" : phase}
          </div>
          <div className="mu">Status</div>
        </div>
      </div>
      <div
        style={{
          marginTop: "var(--space-3)",
          display: "flex",
          gap: 8,
          flexWrap: "wrap",
        }}
      >
        <Link
          to={`/blast/jobs/${jobId}/analytics`}
          className="btn btn--primary btn--sm"
          style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
        >
          <BarChart3 size={14} strokeWidth={1.5} /> View Analytics &amp; Alignments
        </Link>
        {hasExportTargets && (
          <>
            <button
              type="button"
              onClick={() => onExport("csv")}
              disabled={exportingFormat !== null}
              className="btn btn--ghost btn--sm"
              style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
            >
              {exportingFormat === "csv" ? (
                <Loader2 size={12} className="spin" />
              ) : (
                <Download size={12} />
              )}
              CSV
            </button>
            <button
              type="button"
              onClick={() => onExport("json")}
              disabled={exportingFormat !== null}
              className="btn btn--ghost btn--sm"
              style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
            >
              {exportingFormat === "json" ? (
                <Loader2 size={12} className="spin" />
              ) : (
                <Download size={12} />
              )}
              JSON
            </button>
          </>
        )}
      </div>
    </>
  );
}
