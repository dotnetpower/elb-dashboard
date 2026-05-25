import type { ReactNode } from "react";
import { AlertCircle, CheckCircle2, ExternalLink, Loader2, Square } from "lucide-react";

const formatTime = (s: number) => `${Math.floor(s / 60)}m ${s % 60}s`;

/** Human label for a Celery task phase published by provision_aks. Keeps
 *  the FE friendly even though the task strings stay machine-flavoured.
 *  Keep keys in sync with `api/tasks/azure/provision.py::_PROVISION_STEPS`. */
const PHASE_LABELS: Record<string, string> = {
  creating_cluster: "Preparing ARM request",
  ensuring_resource_group: "Ensuring resource group",
  arm_create_or_update: "Creating AKS cluster (5–10 min)",
  ensuring_rbac: "Granting role assignments",
  rbac_ensure_failed_nonfatal: "RBAC partially failed (non-fatal)",
  completed: "Cluster ready",
  failed: "Failing",
};

function prettifyPhase(phase: string | null | undefined): string | null {
  if (!phase) return null;
  return PHASE_LABELS[phase] ?? phase.replace(/_/g, " ");
}

/** Shape of the `progress` payload published by `provision_aks` via
 *  `task.update_state(meta=…)`. All fields are optional — the banner
 *  renders what is present and degrades gracefully when fields are
 *  missing (e.g. first poll before the task started). */
export interface ProvisionProgress {
  phase?: string | null;
  step?: number | null;
  total_steps?: number | null;
  status?: string | null;
  message?: string | null;
  cluster_state?: string | null;
  pools?:
    | {
        name?: string | null;
        state?: string | null;
        count?: number | null;
        vm_size?: string | null;
        mode?: string | null;
      }[]
    | null;
  arm_elapsed_seconds?: number | null;
  rg_visibility_attempt?: number | null;
  rg_visibility_total?: number | null;
  /** Azure portal deep link to the cluster overview blade. Set by the
   *  task once `aks.managed_clusters.get` succeeds (the resource is
   *  visible in ARM), so the user can click through to the portal
   *  while the create is still in flight. */
  portal_url?: string | null;
  updated_at?: string | null;
}

/** Color a pool-state badge per AKS provisioning state strings. */
function poolStateAccent(state: string | null | undefined): {
  color: string;
  border: string;
  bg: string;
  icon: ReactNode;
} {
  const s = (state ?? "").toLowerCase();
  if (s === "succeeded") {
    return {
      color: "var(--success)",
      border: "rgba(106,214,163,0.4)",
      bg: "rgba(106,214,163,0.08)",
      icon: <CheckCircle2 size={11} />,
    };
  }
  if (s === "failed" || s === "canceled" || s === "cancelled") {
    return {
      color: "var(--danger)",
      border: "rgba(255,107,107,0.4)",
      bg: "rgba(255,107,107,0.08)",
      icon: <AlertCircle size={11} />,
    };
  }
  // Creating / Updating / Pending / Deleting / etc.
  return {
    color: "var(--accent)",
    border: "rgba(110,159,255,0.4)",
    bg: "rgba(110,159,255,0.08)",
    icon: <Loader2 size={11} className="spin" />,
  };
}

/** Live "Provisioning..." banner — visible until the cluster appears in the list. */
export function ProvisioningBanner({
  clusterName,
  elapsed,
  nodeCount,
  nodeSku,
  systemNodeCount,
  systemVmSize,
  taskPhase,
  taskProgress,
  onCancel,
  targetResourceGroup,
  targetRegion,
}: {
  clusterName: string;
  elapsed: number;
  nodeCount: number;
  nodeSku: string;
  systemNodeCount: number;
  systemVmSize: string;
  /** Celery phase string (e.g. "arm_create_or_update"). When null the
   *  banner just shows the elapsed time — same UX as before this prop. */
  taskPhase?: string | null;
  /** Rich progress payload (step / total_steps / message / pools / …)
   *  published by `provision_aks`. When null only the basic banner
   *  (phase + elapsed) is rendered. */
  taskProgress?: ProvisionProgress | null;
  /** Cancel the in-flight provisioning task. Renders a small "Stop"
   *  chip on the banner when provided. Omitted when the parent
   *  doesn't wire one — keeps the banner usable in read-only
   *  contexts like the cluster detail page. */
  onCancel?: () => Promise<void> | void;
  /** Target Azure resource group the cluster is being created in.
   *  Surfaced under the cluster name so the user can see when the
   *  cluster is landing in a RG that differs from the dashboard's
   *  Workload RG (a common source of "my cluster disappeared"
   *  confusion). */
  targetResourceGroup?: string;
  /** Target Azure region the cluster is being created in. Shown next
   *  to `targetResourceGroup` for the same reason. */
  targetRegion?: string;
}) {
  const phase = prettifyPhase(taskPhase);
  const step = taskProgress?.step ?? null;
  const totalSteps = taskProgress?.total_steps ?? null;
  const message = taskProgress?.message ?? null;
  const pools = taskProgress?.pools ?? null;
  const armElapsed = taskProgress?.arm_elapsed_seconds ?? null;
  const portalUrl = taskProgress?.portal_url ?? null;

  // Percent for the progress bar. Prefer step/total when present; if we
  // are in the long arm phase, smoothly interpolate within that step
  // using arm_elapsed_seconds (capped at the 10 min mid-point so the bar
  // never claims "almost done" while ARM is still running).
  const percent = (() => {
    if (step && totalSteps && totalSteps > 0) {
      let base = ((step - 1) / totalSteps) * 100;
      let span = (1 / totalSteps) * 100;
      if (taskPhase === "arm_create_or_update" && armElapsed) {
        const fraction = Math.min(armElapsed / (10 * 60), 1);
        base += span * fraction;
        span = 0;
      } else if (taskPhase === "completed") {
        return 100;
      }
      return Math.min(100, Math.round(base + span / 2));
    }
    return null;
  })();

  return (
    <div
      className="glass-card"
      style={{
        padding: "12px 16px",
        marginBottom: "var(--space-3)",
        border: "1px solid rgba(110,159,255,0.25)",
        background: "rgba(110,159,255,0.04)",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          gap: 12,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
          <Loader2 size={16} className="spin" style={{ color: "var(--accent)" }} />
          <div style={{ minWidth: 0 }}>
            <div
              style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}
            >
              {clusterName}
            </div>
            {(targetResourceGroup || targetRegion) && (
              <div
                style={{
                  fontSize: 11,
                  color: "var(--text-muted)",
                  marginTop: 2,
                }}
              >
                {targetResourceGroup ? (
                  <span>rg: <strong>{targetResourceGroup}</strong></span>
                ) : null}
                {targetResourceGroup && targetRegion ? " · " : ""}
                {targetRegion ? <span>region: <strong>{targetRegion}</strong></span> : null}
              </div>
            )}
            <div style={{ fontSize: 11, color: "var(--accent)" }}>
              {step && totalSteps ? `Step ${step}/${totalSteps} · ` : ""}
              {phase ?? "Provisioning..."}
              {" · "}
              {formatTime(elapsed)}
            </div>
          </div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
            blastpool {nodeCount} × {nodeSku}
          </div>
          <div style={{ fontSize: 10, color: "var(--text-faint)" }}>
            systempool {systemNodeCount} × {systemVmSize} · Est. 5–10 min
          </div>
        </div>
      </div>

      {/* Sub-progress: message + step progress bar + per-pool chips. All
          three are independent — render whichever data is present. */}
      {(message || percent !== null) && (
        <div style={{ marginTop: 10 }}>
          {message && (
            <div
              style={{
                fontSize: 11,
                color: "var(--text-muted)",
                marginBottom: 6,
              }}
            >
              {message}
              {armElapsed != null && taskPhase === "arm_create_or_update"
                ? ` · ARM ${formatTime(armElapsed)}`
                : ""}
            </div>
          )}
          {percent !== null && (
            <div
              style={{
                height: 4,
                borderRadius: 2,
                background: "rgba(110,159,255,0.12)",
                overflow: "hidden",
              }}
            >
              <div
                style={{
                  width: `${percent}%`,
                  height: "100%",
                  background: "var(--accent)",
                  transition: "width 400ms ease-out",
                }}
              />
            </div>
          )}
        </div>
      )}

      {pools && pools.length > 0 && (
        <div
          style={{
            marginTop: 10,
            display: "flex",
            flexWrap: "wrap",
            gap: 6,
          }}
        >
          {pools.map((p, idx) => {
            const accent = poolStateAccent(p.state);
            return (
              <div
                key={`${p.name ?? "pool"}-${idx}`}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 4,
                  fontSize: 10,
                  padding: "2px 8px",
                  borderRadius: 10,
                  border: `1px solid ${accent.border}`,
                  background: accent.bg,
                  color: accent.color,
                }}
                title={p.vm_size ?? ""}
              >
                {accent.icon}
                <span style={{ fontWeight: 600 }}>{p.name ?? "pool"}</span>
                <span style={{ color: "var(--text-muted)" }}>·</span>
                <span>{p.state ?? "Pending"}</span>
                {p.count != null && (
                  <>
                    <span style={{ color: "var(--text-muted)" }}>·</span>
                    <span>{p.count}n</span>
                  </>
                )}
              </div>
            );
          })}
        </div>
      )}

      {(portalUrl || onCancel) && (
        <div style={{ marginTop: 10, display: "flex", flexWrap: "wrap", gap: 6 }}>
          {portalUrl && (
            <a
              href={portalUrl}
              target="_blank"
              rel="noopener noreferrer"
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
                fontSize: 11,
                color: "var(--accent)",
                textDecoration: "none",
                padding: "3px 8px",
                borderRadius: 6,
                border: "1px solid rgba(110,159,255,0.3)",
                background: "rgba(110,159,255,0.05)",
              }}
            >
              <ExternalLink size={11} strokeWidth={1.5} />
              Open in Azure portal
            </a>
          )}
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
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
                fontSize: 11,
                color: "var(--warning)",
                cursor: "pointer",
                padding: "3px 8px",
                borderRadius: 6,
                border: "1px solid rgba(229,182,102,0.35)",
                background: "rgba(229,182,102,0.05)",
              }}
              title="Send a cancel signal to the Celery worker"
            >
              <Square size={11} strokeWidth={1.5} />
              Stop provisioning
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export function ProvisionDoneBanner({
  clusterName,
  roleResult,
}: {
  clusterName: string;
  roleResult?: string[] | null;
}) {
  return (
    <div
      style={{
        padding: "8px 12px",
        background: "rgba(106,214,163,0.06)",
        border: "1px solid rgba(106,214,163,0.2)",
        borderRadius: 8,
        fontSize: 12,
        color: "var(--success)",
        marginBottom: "var(--space-3)",
        display: "flex",
        alignItems: "center",
        gap: 6,
      }}
    >
      <CheckCircle2 size={14} /> Cluster <strong>{clusterName}</strong> is ready.
      {roleResult && roleResult.length > 0 && (
        <span className="muted" style={{ fontSize: 11 }}>
          {" "}
          · Roles: {roleResult.join(", ")}
        </span>
      )}
    </div>
  );
}
