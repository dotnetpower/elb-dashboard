import { createPortal } from "react-dom";
import { Loader2, RefreshCw, Terminal, X } from "lucide-react";

import { LogHighlighter } from "./LogHighlighter";

/**
 * Modal that displays the last 200 lines of a pod's logs with syntax
 * coloring. Pure presentation — the parent owns the fetch lifecycle.
 */
export interface PodLogsDialogProps {
  target: { namespace: string; pod: string };
  output: string | null;
  loading: boolean;
  onRefresh: () => void;
  onClose: () => void;
}

export function PodLogsDialog({
  target,
  output,
  loading,
  onRefresh,
  onClose,
}: PodLogsDialogProps) {
  return createPortal(
    <div
      className="glass-dialog-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      role="dialog"
      aria-modal="true"
      aria-label={`Logs: ${target.pod}`}
    >
      <div
        className="glass-card glass-card--strong glass-dialog"
        onClick={(e) => e.stopPropagation()}
        style={{
          maxWidth: 1100,
          width: "calc(100vw - 48px)",
          maxHeight: "90vh",
          display: "flex",
          flexDirection: "column",
          padding: 0,
          overflow: "hidden",
          textAlign: "left",
        }}
      >
        <div
          style={{
            padding: "14px 20px",
            background:
              "linear-gradient(135deg, rgba(92,202,180,0.08) 0%, rgba(110,159,255,0.06) 100%)",
            borderBottom: "1px solid var(--border-weak)",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <div
              style={{
                width: 28,
                height: 28,
                borderRadius: 8,
                background: "linear-gradient(135deg, var(--teal), var(--accent))",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                boxShadow: "0 2px 8px rgba(92,202,180,0.25)",
              }}
            >
              <Terminal size={14} style={{ color: "#fff" }} />
            </div>
            <div>
              <div style={{ fontSize: 13, fontWeight: 600 }}>Pod Logs</div>
              <div style={{ fontSize: 10, color: "var(--text-muted)" }}>
                {target.namespace} / {target.pod} · last 200 lines
              </div>
            </div>
          </div>
          <div style={{ display: "flex", gap: 6 }}>
            <button
              className="glass-button"
              onClick={onRefresh}
              disabled={loading}
              style={{
                padding: "5px 10px",
                fontSize: 10,
                display: "flex",
                alignItems: "center",
                gap: 4,
              }}
            >
              <RefreshCw size={11} className={loading ? "spin" : ""} /> Refresh
            </button>
            <button
              className="glass-button"
              onClick={onClose}
              style={{ padding: "5px 8px", border: "none" }}
            >
              <X size={16} />
            </button>
          </div>
        </div>
        <div
          style={{
            margin: 0,
            padding: "14px 20px",
            flex: 1,
            overflow: "auto",
            fontSize: 11,
            lineHeight: 1.7,
            background: "#0d1117",
            fontFamily: "var(--font-mono)",
            color: "#c9d1d9",
            textAlign: "left",
          }}
        >
          {loading ? (
            <span style={{ color: "var(--text-faint)" }}>
              <Loader2 size={11} className="spin" /> Fetching logs...
            </span>
          ) : (
            <LogHighlighter text={output ?? ""} />
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
