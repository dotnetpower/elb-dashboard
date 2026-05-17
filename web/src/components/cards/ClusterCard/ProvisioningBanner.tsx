import { CheckCircle2, Loader2 } from "lucide-react";

const formatTime = (s: number) => `${Math.floor(s / 60)}m ${s % 60}s`;

/** Live "Provisioning..." banner — visible until the cluster appears in the list. */
export function ProvisioningBanner({
  clusterName,
  elapsed,
  nodeCount,
  nodeSku,
  systemNodeCount,
  systemVmSize,
}: {
  clusterName: string;
  elapsed: number;
  nodeCount: number;
  nodeSku: string;
  systemNodeCount: number;
  systemVmSize: string;
}) {
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
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Loader2 size={16} className="spin" style={{ color: "var(--accent)" }} />
          <div>
            <div
              style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}
            >
              {clusterName}
            </div>
            <div style={{ fontSize: 11, color: "var(--accent)" }}>
              Provisioning... {formatTime(elapsed)}
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
