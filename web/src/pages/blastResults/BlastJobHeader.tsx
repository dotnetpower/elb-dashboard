import { Link, useNavigate } from "react-router-dom";
import { ArrowLeft, Clock, Copy, Download, StopCircle } from "lucide-react";

import { ElapsedTimer } from "@/components/BlastFilePreview";
import { useToast } from "@/components/Toast";
import {
  buildConfigFilename,
  downloadConfigJson,
  partialFormFromJobPayload,
  PENDING_DUPLICATE_KEY,
  serializeFormToConfig,
  type BlastConfigSnapshot,
  type ExportableFormFields,
} from "@/pages/blastSubmit/configSerializer";
import { INITIAL, type FormState } from "@/pages/blastSubmitModel";

interface BlastJobHeaderProps {
  jobId: string;
  jobTitle: string | null;
  createdAt: string | null;
  isRunning: boolean;
  cancelDisabled: boolean;
  onRequestCancel: () => void;
  /**
   * Original submit payload (BlastSubmitRequest body) — drives the
   * Duplicate / Re-run + Export config actions. Absent on legacy jobs
   * that pre-date payload persistence; the buttons stay disabled in that
   * case to keep the affordance discoverable without misleading the user.
   */
  jobPayload?: Record<string, unknown> | undefined;
}

/**
 * Top of the BLAST results page — back link, job title, live elapsed timer
 * (only while the job is running), Cancel button, and the
 * Duplicate / Export-config actions. The actual cancel mutation is owned
 * by the parent so the confirm dialog and post-cancel toasts can sit next
 * to the rest of the page state.
 */
export function BlastJobHeader({
  jobId,
  jobTitle,
  createdAt,
  isRunning,
  cancelDisabled,
  onRequestCancel,
  jobPayload,
}: BlastJobHeaderProps) {
  const navigate = useNavigate();
  const { toast } = useToast();
  const hydratableFields = jobPayload
    ? partialFormFromJobPayload(jobPayload)
    : null;
  const canReuseConfig = Boolean(hydratableFields);

  const handleDuplicate = () => {
    if (!hydratableFields) return;
    try {
      window.sessionStorage.setItem(
        PENDING_DUPLICATE_KEY,
        JSON.stringify({
          source: { jobId, jobTitle: jobTitle ?? undefined },
          form: hydratableFields,
        }),
      );
    } catch (err) {
      // Storage quota / disabled storage — fall through with a toast so
      // the user knows the action didn't silently no-op.
      toast(
        `Could not stash duplicate config: ${
          err instanceof Error ? err.message : "storage unavailable"
        }`,
        "error",
      );
      return;
    }
    toast("Configuration copied to BLAST submit form.", "success");
    navigate("/blast/submit");
  };

  const handleExport = () => {
    if (!hydratableFields) return;
    const fullForm: FormState = { ...INITIAL, ...(hydratableFields as Partial<FormState>) };
    const snapshot: BlastConfigSnapshot = serializeFormToConfig({
      form: fullForm,
      source: { jobId, jobTitle: jobTitle ?? undefined },
    });
    try {
      downloadConfigJson(
        snapshot,
        buildConfigFilename({ jobId, jobTitle: jobTitle ?? undefined }),
      );
      toast("Config JSON downloaded.", "success");
    } catch (err) {
      toast(
        `Export failed: ${err instanceof Error ? err.message : "unknown error"}`,
        "error",
      );
    }
  };

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
        <button
          className="glass-button"
          onClick={handleDuplicate}
          disabled={!canReuseConfig}
          title={
            canReuseConfig
              ? "Pre-fill the BLAST submit form with this job's parameters."
              : "Original submit payload is not available for this job."
          }
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 5,
            fontSize: 12,
          }}
        >
          <Copy size={14} strokeWidth={1.5} /> Duplicate
        </button>
        <button
          className="glass-button"
          onClick={handleExport}
          disabled={!canReuseConfig}
          title={
            canReuseConfig
              ? "Download this job's configuration as a JSON file."
              : "Original submit payload is not available for this job."
          }
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 5,
            fontSize: 12,
          }}
        >
          <Download size={14} strokeWidth={1.5} /> Export config
        </button>
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

// Re-exported so the parent doesn't need a second import line just to
// satisfy strict TypeScript when introspecting the hydrated shape.
export type { ExportableFormFields };
