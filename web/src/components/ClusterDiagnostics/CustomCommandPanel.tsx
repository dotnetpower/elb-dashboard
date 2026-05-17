import { useState, useCallback } from "react";
import { Loader2, Terminal } from "lucide-react";

import { monitoringApi } from "@/api/endpoints";

/**
 * Read-only kubectl runner. The backend allowlist (`get`, `top`,
 * `describe`, `logs`) is enforced server-side; this component is
 * responsible only for the input + output rendering.
 */
export function CustomCommandPanel({
  subscriptionId,
  resourceGroup,
  clusterName,
}: {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
}) {
  const [customCmd, setCustomCmd] = useState("");
  const [customResult, setCustomResult] = useState<{
    output: string;
    exit_code: number;
  } | null>(null);
  const [customLoading, setCustomLoading] = useState(false);

  const runCustom = useCallback(async () => {
    if (!customCmd.trim()) return;
    setCustomLoading(true);
    try {
      const result = await monitoringApi.runAksCommand(
        subscriptionId,
        resourceGroup,
        clusterName,
        customCmd.trim(),
      );
      setCustomResult(result);
    } catch (e) {
      setCustomResult({ output: (e as Error).message, exit_code: -1 });
    } finally {
      setCustomLoading(false);
    }
  }, [customCmd, subscriptionId, resourceGroup, clusterName]);

  return (
    <div
      style={{
        borderRadius: 8,
        border: "1px solid var(--border-weak)",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          padding: "8px 12px",
          background: "var(--bg-tertiary)",
          fontSize: 10,
          fontWeight: 500,
          display: "flex",
          alignItems: "center",
          gap: 6,
          borderBottom: "1px solid var(--border-weak)",
        }}
      >
        <Terminal size={12} strokeWidth={1.5} /> Run kubectl command
        <span className="muted" style={{ fontSize: 9, marginLeft: "auto" }}>
          read-only: get, top, describe, logs
        </span>
      </div>
      <div style={{ padding: "10px 12px", display: "flex", gap: 8 }}>
        <input
          type="text"
          value={customCmd}
          onChange={(e) => setCustomCmd(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") runCustom();
          }}
          placeholder="kubectl get svc -A"
          style={{
            flex: 1,
            fontSize: 12,
            padding: "7px 10px",
            background: "var(--bg-canvas)",
            border: "1px solid var(--border-weak)",
            borderRadius: 6,
            color: "var(--text-primary)",
            fontFamily: "var(--font-mono)",
          }}
          spellCheck={false}
        />
        <button
          className="glass-button glass-button--primary"
          onClick={runCustom}
          disabled={customLoading || !customCmd.trim()}
          style={{ fontSize: 10, padding: "4px 10px" }}
        >
          {customLoading ? <Loader2 size={10} className="spin" /> : "Run"}
        </button>
      </div>
      {customResult && (
        <pre
          style={{
            margin: 0,
            padding: "10px 12px",
            fontSize: 11,
            lineHeight: 1.5,
            background: "var(--bg-canvas)",
            borderTop: "1px solid var(--border-weak)",
            overflow: "auto",
            maxHeight: 250,
            whiteSpace: "pre-wrap",
            wordBreak: "break-all",
            color:
              customResult.exit_code === 0 ? "var(--text-primary)" : "var(--danger)",
            fontFamily: "var(--font-mono)",
          }}
        >
          {customResult.output || "(no output)"}
        </pre>
      )}
    </div>
  );
}
