import { Link } from "react-router-dom";
import { ArrowLeft, Clock, StopCircle } from "lucide-react";

import { ElapsedTimer } from "@/components/BlastFilePreview";

interface BlastJobHeaderProps {
  jobId: string;
  jobTitle: string | null;
  createdAt: string | null;
  isRunning: boolean;
  cancelDisabled: boolean;
  onRequestCancel: () => void;
}

/**
 * Top of the BLAST results page — back link, job title, live elapsed timer
 * (only while the job is running), and the Cancel button.
 *
 * The actual cancel mutation is owned by the parent so the confirm dialog and
 * post-cancel toasts can sit next to the rest of the page state.
 */
export function BlastJobHeader({
  jobId,
  jobTitle,
  createdAt,
  isRunning,
  cancelDisabled,
  onRequestCancel,
}: BlastJobHeaderProps) {
  return (
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
      <div style={{ display: "flex", alignItems: "center", gap: "var(--space-3)" }}>
        <h1 style={{ margin: 0, flex: 1 }}>{jobTitle || jobId}</h1>
        {createdAt && isRunning && (
          <span
            style={{
              fontSize: 12,
              color: "var(--text-muted)",
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
            }}
          >
            <Clock size={12} strokeWidth={1.5} />
            <ElapsedTimer startTime={createdAt} />
          </span>
        )}
        {isRunning && (
          <button
            className="glass-button"
            onClick={onRequestCancel}
            disabled={cancelDisabled}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 5,
              fontSize: 12,
              color: "var(--danger)",
            }}
          >
            <StopCircle size={14} strokeWidth={1.5} /> Cancel
          </button>
        )}
      </div>
    </header>
  );
}
