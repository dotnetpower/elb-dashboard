import type { ResourceConfig } from "../types";

export function Step4Confirm({ config }: { config: ResourceConfig }) {
  const rows: ReadonlyArray<readonly [string, string]> = [
    ["Subscription", config.subscriptionId],
    ["Primary Region", config.region || "— (auto-detected)"],
    ["Workload RG", config.workloadResourceGroup],
    ["Storage", config.storageAccountName || "— (skip)"],
    ["ACR RG", config.acrResourceGroup],
    ["ACR", config.acrName || "— (skip)"],
    ["Terminal", "in-process sidecar (no VM)"],
  ];
  return (
    <div>
      <h2 style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>
        Confirm Setup
      </h2>
      <p
        style={{
          fontSize: 12,
          color: "var(--text-muted)",
          marginBottom: 14,
          lineHeight: 1.5,
        }}
      >
        Review your configuration.
      </p>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
        <tbody>
          {rows.map(([l, v]) => (
            <tr key={l}>
              <td
                style={{
                  padding: "8px 0",
                  borderBottom: "1px solid var(--border-weak)",
                  color: "var(--text-muted)",
                  width: 160,
                }}
              >
                {l}
              </td>
              <td
                style={{
                  padding: "8px 0",
                  borderBottom: "1px solid var(--border-weak)",
                  fontWeight: 500,
                  fontFamily: "var(--font-mono)",
                  fontSize: 12,
                  color: v.startsWith("—")
                    ? "var(--text-faint)"
                    : "var(--text-primary)",
                }}
              >
                {v}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
