import type { AksAgentPool } from "@/api/endpoints";

/** Per-pool table — one row per agent pool with SKU, count, OS, mode, autoscale, state. */
export function NodePoolsTable({ agentPools }: { agentPools: AksAgentPool[] }) {
  return (
    <div style={{ marginBottom: 20 }}>
      <div
        style={{
          fontSize: 11,
          fontWeight: 600,
          marginBottom: 8,
          display: "flex",
          alignItems: "center",
          gap: 6,
        }}
      >
        <span
          style={{
            width: 3,
            height: 14,
            borderRadius: 2,
            background: "var(--accent)",
          }}
        />
        Node Pools
      </div>
      <div
        className="cluster-detail-pools-wrap"
        style={{
          borderRadius: 8,
          border: "1px solid var(--border-weak)",
          overflow: "hidden",
        }}
      >
        <table style={{ width: "100%", fontSize: 11, borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ background: "var(--modal-thead-bg)" }}>
              {["Pool", "SKU", "Nodes", "OS", "Mode", "Autoscale", "State"].map((h) => (
                <th
                  key={h}
                  style={{
                    textAlign: h === "Nodes" ? "center" : "left",
                    padding: "8px 10px",
                    color: "var(--text-faint)",
                    fontSize: 9,
                    textTransform: "uppercase",
                    letterSpacing: "0.05em",
                    fontWeight: 500,
                  }}
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {agentPools.map((p, i) => (
              <tr
                key={p.name}
                style={{
                  background:
                    i % 2 === 0 ? "transparent" : "var(--modal-zebra)",
                  borderTop: "1px solid var(--border-weak)",
                }}
              >
                <td style={{ padding: "8px 10px", fontWeight: 600 }}>{p.name}</td>
                <td style={{ padding: "8px 10px" }}>
                  <code style={{ fontSize: 10 }}>{p.vm_size}</code>
                </td>
                <td
                  style={{
                    padding: "8px 10px",
                    textAlign: "center",
                    fontWeight: 600,
                  }}
                >
                  {p.count}
                </td>
                <td style={{ padding: "8px 10px" }}>{p.os_type}</td>
                <td style={{ padding: "8px 10px" }}>
                  <span
                    style={{
                      fontSize: 9,
                      padding: "2px 6px",
                      borderRadius: 4,
                      background:
                        p.mode === "System"
                          ? "rgba(110,159,255,0.1)"
                          : "rgba(115,191,105,0.1)",
                      color:
                        p.mode === "System" ? "var(--accent)" : "var(--success)",
                    }}
                  >
                    {p.mode}
                  </span>
                </td>
                <td style={{ padding: "8px 10px", fontSize: 10 }}>
                  {p.enable_auto_scaling ? (
                    <span style={{ color: "var(--success)" }}>
                      {p.min_count}–{p.max_count}
                    </span>
                  ) : (
                    <span className="muted">Off</span>
                  )}
                </td>
                <td style={{ padding: "8px 10px" }}>
                  <span
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 4,
                      fontSize: 10,
                      fontWeight: 500,
                      color:
                        p.power_state === "Running"
                          ? "var(--success)"
                          : "var(--warning)",
                    }}
                  >
                    <span
                      style={{
                        width: 5,
                        height: 5,
                        borderRadius: "50%",
                        background:
                          p.power_state === "Running"
                            ? "var(--success)"
                            : "var(--warning)",
                      }}
                    />
                    {p.power_state ?? "?"}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
