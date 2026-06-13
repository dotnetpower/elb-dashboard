/**
 * AKS provision modal — live provisioning status panel.
 *
 * Visible while the Celery `provision_aks` task is creating the cluster
 * but ARM has not yet accepted it. Mirrors the dashboard banner (step
 * counter, phase, elapsed) inside the still-open modal so an ARM
 * rejection surfaces inline with the user's inputs intact. Pure
 * presentation; the parent owns the task lifecycle and cancel wiring.
 */

import { Loader2, Square } from "lucide-react";

import type { ProvisionProgress } from "./ProvisioningBanner";

export function ProvisioningStatusPanel({
  provStatus,
  taskProgress,
  taskPhase,
  elapsed,
  onCancel,
}: {
  provStatus: string;
  taskProgress: ProvisionProgress | null;
  taskPhase: string | null;
  elapsed: number;
  onCancel?: () => Promise<void> | void;
}) {
  if (provStatus !== "creating") return null;
  return (
    <div
      style={{
        fontSize: 12,
        color: "var(--text-primary)",
        display: "flex",
        flexDirection: "column",
        gap: 6,
        padding: "8px 10px",
        borderRadius: 8,
        border: "1px solid rgba(110,159,255,0.3)",
        background: "rgba(110,159,255,0.06)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          justifyContent: "space-between",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
          <Loader2 size={12} strokeWidth={1.5} className="spin" />
          <span style={{ fontWeight: 600 }}>
            {taskProgress?.step && taskProgress?.total_steps
              ? `Step ${taskProgress.step}/${taskProgress.total_steps}`
              : "Provisioning"}
          </span>
          <span style={{ color: "var(--text-muted)" }}>
            · {taskPhase ?? "in progress"} ·{" "}
            {Math.floor(elapsed / 60)}m {elapsed % 60}s
          </span>
        </div>
        {onCancel && (
          <button
            type="button"
            onClick={() => {
              if (
                window.confirm(
                  "Stop provisioning? The cluster create may already be in flight on Azure; you may still need to delete a partial cluster manually.",
                )
              ) {
                void onCancel();
              }
            }}
            className="glass-button"
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              fontSize: 11,
              padding: "3px 8px",
              color: "var(--warning)",
              whiteSpace: "nowrap",
            }}
            title="Send a cancel signal to the Celery worker"
          >
            <Square size={11} strokeWidth={1.5} />
            Stop
          </button>
        )}
      </div>
      {taskProgress?.message && (
        <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
          {taskProgress.message}
        </div>
      )}
      <div style={{ fontSize: 10, color: "var(--text-faint)" }}>
        The modal will close automatically as soon as Azure accepts
        the cluster create. If validation fails, the error will
        appear here with your inputs preserved.
      </div>
    </div>
  );
}
