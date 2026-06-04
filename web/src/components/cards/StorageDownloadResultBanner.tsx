import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Loader2, X } from "lucide-react";

import { formatNcbiVersion } from "@/components/cards/storageDbCatalog";

export function StorageDownloadResultBanner({
  result,
  onDismiss,
}: {
  result: { db: string; msg: string; version?: string; type: "ok" | "err" | "pending" };
  onDismiss: () => void;
}) {
  const [fading, setFading] = useState(false);

  useEffect(() => {
    // Errors stay until dismissed; pending banners are replaced by the
    // terminal ok/err result when the request resolves, so neither
    // auto-dismisses.
    if (result.type !== "ok") return;
    const fadeTimer = setTimeout(() => setFading(true), 3000);
    const removeTimer = setTimeout(onDismiss, 3500);
    return () => {
      clearTimeout(fadeTimer);
      clearTimeout(removeTimer);
    };
  }, [result, onDismiss]);

  return (
    <div
      style={{
        marginBottom: "var(--space-3)",
        padding: "8px 12px",
        borderRadius: 8,
        fontSize: 12,
        background:
          result.type === "ok"
            ? "rgba(115,191,105,0.08)"
            : result.type === "pending"
              ? "rgba(110,159,255,0.08)"
              : "rgba(242,114,111,0.08)",
        border: `1px solid ${
          result.type === "ok"
            ? "rgba(115,191,105,0.25)"
            : result.type === "pending"
              ? "rgba(110,159,255,0.28)"
              : "rgba(242,114,111,0.25)"
        }`,
        color:
          result.type === "ok"
            ? "var(--success)"
            : result.type === "pending"
              ? "var(--accent)"
              : "var(--danger)",
        display: "flex",
        alignItems: "center",
        gap: 8,
        opacity: fading ? 0 : 1,
        transition: "opacity 0.5s ease-out",
      }}
    >
      {result.type === "ok" ? (
        <CheckCircle2 size={14} style={{ flexShrink: 0 }} />
      ) : result.type === "pending" ? (
        <Loader2 size={14} className="spin" style={{ flexShrink: 0 }} />
      ) : (
        <AlertTriangle size={14} style={{ flexShrink: 0 }} />
      )}
      <div style={{ flex: 1 }}>
        <strong>{result.db}</strong>: {result.msg}
        {result.version && (
          <span
            style={{
              marginLeft: 8,
              fontSize: 10,
              color: "var(--text-faint)",
              fontWeight: 400,
            }}
          >
            Version: {formatNcbiVersion(result.version)}
          </span>
        )}
      </div>
      <button
        onClick={onDismiss}
        style={{
          background: "none",
          border: "none",
          color: "inherit",
          cursor: "pointer",
          padding: 2,
          opacity: 0.6,
        }}
      >
        <X size={12} />
      </button>
    </div>
  );
}