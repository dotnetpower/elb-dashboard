/**
 * AKS provision modal — pre-flight checklist panel.
 *
 * Renders the per-check result rows (SKU availability, quota, resource
 * group, region, RBAC) from the latest `/api/aks/preflight` call, with
 * inline remediations: "Apply N nodes" to fit quota and per-missing-role
 * "Copy grant command". Pure presentation; the parent `ProvisionModal`
 * owns the preflight call and node-count state.
 */

import { AlertCircle, AlertTriangle, CheckCircle2, Copy, Loader2 } from "lucide-react";

import type { AksPreflightResponse } from "@/api/endpoints";

export function PreflightChecklist({
  preflightStatus,
  showPreflightChecking,
  preflightResult,
  nodeCount,
  setNodeCount,
}: {
  preflightStatus: "idle" | "checking" | "done";
  showPreflightChecking: boolean;
  preflightResult: AksPreflightResponse | null;
  nodeCount: number;
  setNodeCount: (v: number) => void;
}) {
  return (
    <>
      {/* Pre-flight progress list. The user sees one row per check
          (skus / quota / resource_group) with an icon + message.
          Renders only after the first Create click so the modal
          stays clean on first open. `fail` rows block submit (the
          button below switches to "Re-check"); `warn` rows pass
          through. */}
      {preflightStatus === "checking" && showPreflightChecking && (
        <div
          style={{
            fontSize: 12,
            color: "var(--text-muted)",
            display: "flex",
            alignItems: "center",
            gap: 6,
          }}
        >
          <Loader2 size={12} strokeWidth={1.5} className="spin" />
          Validating with Azure (SKU availability, quota, resource group)…
        </div>
      )}
      {preflightStatus === "done" && preflightResult && (
        <div
          style={{
            fontSize: 11,
            display: "flex",
            flexDirection: "column",
            gap: 4,
            padding: "8px 10px",
            borderRadius: 8,
            border: `1px solid ${
              preflightResult.ok
                ? "rgba(106,214,163,0.3)"
                : "rgba(255,107,107,0.35)"
            }`,
            background: preflightResult.ok
              ? "rgba(106,214,163,0.06)"
              : "rgba(255,107,107,0.06)",
          }}
        >
          {preflightResult.checks.map((c) => {
            const color =
              c.status === "ok"
                ? "var(--success)"
                : c.status === "warn"
                  ? "var(--warning)"
                  : "var(--danger)";
            const Icon =
              c.status === "ok"
                ? CheckCircle2
                : c.status === "warn"
                  ? AlertTriangle
                  : AlertCircle;
            const labelById: Record<string, string> = {
              skus: "VM SKU availability",
              quota: "Compute quota",
              resource_group: "Resource group",
              region: "Region",
              rbac: "Dashboard MI permissions",
            };
            // The quota row carries a recommended `max_blast_nodes_fit`
            // when it fails. If that number is >= 1 and differs from
            // the user's current pick, render an inline "Apply N
            // nodes" button so the user can fit-to-quota in one
            // click. `max_blast_nodes_fit === 0` means the
            // requested SKU's per-node core count already exceeds
            // the available headroom — in that case there is no
            // useful fit and we show no button (only the message).
            const isQuotaFail = c.name === "quota" && c.status === "fail";
            const maxFit = isQuotaFail
              ? Number(
                  (c.details as { max_blast_nodes_fit?: number } | undefined)
                    ?.max_blast_nodes_fit ?? 0,
                )
              : 0;
            const canApply =
              isQuotaFail && maxFit >= 1 && maxFit !== nodeCount;
            // RBAC row carries `details.missing[]` when status === "fail".
            // Each entry has `{scope, role, reason, remediation}` —
            // the remediation is a ready-to-run `az role assignment
            // create` (or "re-run azd up" for the project custom
            // role). Surface one Copy button per missing item so the
            // operator can paste-and-fix without hunting through
            // logs or docs.
            const rbacMissing =
              c.name === "rbac" && c.status === "fail"
                ? ((c.details as { missing?: Array<{ scope: string; role: string; reason: string; remediation: string }> } | undefined)?.missing ?? [])
                : [];
            return (
              <div
                key={c.name}
                style={{
                  display: "flex",
                  alignItems: "flex-start",
                  gap: 6,
                  color: "var(--text-primary)",
                }}
              >
                <Icon
                  size={12}
                  strokeWidth={1.5}
                  style={{ color, marginTop: 1, flexShrink: 0 }}
                />
                <div style={{ minWidth: 0, flex: 1 }}>
                  <span style={{ fontWeight: 600 }}>
                    {labelById[c.name] ?? c.name}
                  </span>{" "}
                  <span style={{ color: "var(--text-muted)" }}>
                    — {c.message}
                  </span>
                  {canApply && (
                    <button
                      type="button"
                      onClick={() => setNodeCount(maxFit)}
                      className="glass-button"
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 4,
                        fontSize: 10,
                        padding: "2px 8px",
                        marginLeft: 8,
                        color: "var(--accent)",
                      }}
                      title={`Set Node Count to ${maxFit} to fit your current quota`}
                    >
                      Apply {maxFit} nodes
                    </button>
                  )}
                  {isQuotaFail && maxFit === 0 && (
                    <span
                      style={{
                        display: "inline-block",
                        marginLeft: 8,
                        fontSize: 10,
                        color: "var(--warning)",
                      }}
                    >
                      (no node count fits — switch SKU or request quota)
                    </span>
                  )}
                  {rbacMissing.length > 0 && (
                    <div
                      style={{
                        marginTop: 6,
                        display: "flex",
                        flexDirection: "column",
                        gap: 6,
                      }}
                    >
                      {rbacMissing.map((m, idx) => (
                        <div
                          key={`${m.scope}-${m.role}-${idx}`}
                          style={{
                            fontSize: 11,
                            color: "var(--text-muted)",
                            paddingLeft: 8,
                            borderLeft: "2px solid var(--danger)",
                          }}
                        >
                          <div>
                            <span style={{ fontWeight: 600, color: "var(--text-primary)" }}>
                              {m.role}
                            </span>{" "}
                            missing at <code>{m.scope}</code>
                          </div>
                          <div style={{ marginTop: 2 }}>{m.reason}</div>
                          <button
                            type="button"
                            onClick={() => {
                              void navigator.clipboard.writeText(m.remediation);
                            }}
                            className="glass-button"
                            style={{
                              display: "inline-flex",
                              alignItems: "center",
                              gap: 4,
                              fontSize: 10,
                              padding: "2px 8px",
                              marginTop: 4,
                              color: "var(--accent)",
                            }}
                            title="Copy the remediation command for an Owner / User Access Administrator to run"
                          >
                            <Copy size={10} strokeWidth={1.5} />
                            Copy grant command
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}
