import { useTransientState } from "../../hooks/useTransientState";
import { Check, CircleDot, Clock, Copy, Download } from "lucide-react";

import { JsonHighlight } from "@/pages/apiReference/JsonHighlight";
import { statusColor } from "@/pages/apiReference/spec";

export function ResponseViewer({
  response,
  onCopy,
  onDownload,
}: {
  response: { status: number; body: string; time: number; filename?: string };
  onCopy: () => void;
  onDownload?: () => void;
}) {
  const [copied, flashCopied] = useTransientState(false);
  const isOk = response.status >= 200 && response.status < 300;
  const borderColor = isOk ? "rgba(115,191,105,0.25)" : "rgba(242,114,111,0.25)";

  const doCopy = () => {
    onCopy();
    flashCopied(true);
  };

  return (
    <div
      style={{
        borderRadius: 8,
        overflow: "hidden",
        border: `1px solid ${borderColor}`,
        background: "var(--bg-secondary)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "8px 14px",
          background: isOk ? "rgba(115,191,105,0.04)" : "rgba(242,114,111,0.04)",
          borderBottom: `1px solid ${borderColor}`,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <CircleDot size={10} style={{ color: statusColor(response.status) }} />
          <span
            style={{
              fontSize: 13,
              fontWeight: 700,
              fontFamily: "var(--font-mono)",
              color: statusColor(response.status),
            }}
          >
            {response.status || "Error"}
          </span>
          <span
            style={{
              fontSize: 11,
              color: "var(--text-faint)",
              display: "flex",
              alignItems: "center",
              gap: 3,
            }}
          >
            <Clock size={10} /> {response.time}ms
          </span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <button
            type="button"
            className="glass-button"
            onClick={doCopy}
            style={{ padding: "3px 8px", fontSize: 10 }}
          >
            {copied ? (
              <>
                <Check size={10} /> Copied
              </>
            ) : (
              <>
                <Copy size={10} /> Copy
              </>
            )}
          </button>
          {onDownload && (
            <button
              type="button"
              className="glass-button glass-button--primary"
              onClick={onDownload}
              title={
                response.filename
                  ? `Download response as ${response.filename}`
                  : "Download response as a file"
              }
              style={{ padding: "3px 8px", fontSize: 10 }}
            >
              <Download size={10} /> Download
            </button>
          )}
        </div>
      </div>
      <pre
        className="openapi-response__body"
        style={{
          margin: 0,
          padding: "12px 14px",
          fontSize: 11,
          lineHeight: 1.6,
          maxHeight: 360,
          overflow: "auto",
          whiteSpace: "pre-wrap",
          overflowWrap: "anywhere",
          color: "var(--text-primary)",
          fontFamily: "var(--font-mono)",
          background: "transparent",
        }}
      >
        <JsonHighlight text={response.body} />
      </pre>
    </div>
  );
}