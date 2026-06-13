/**
 * Sidecar HTTP request inspector — request detail surfaces.
 *
 * `DetailContent` (shared field grid + headers/body blocks),
 * `InlineRequestDetail` (table drill-down panel), and `Drawer`
 * (right-side panel opened from the scatter chart).
 */

import { useMemo } from "react";
import { X } from "lucide-react";
import type { MockReq } from "./types";
import {
  buildCurl,
  fmtAgo,
  fmtBytes,
  fmtMs,
  fmtTime,
  headerValue,
  latencyTone,
} from "./format";
import { DegradedPill, KvBlock, MethodPill, Row, SectionHeader, StatusPill } from "./atoms";
import { CodeBlock, CopyActionButton } from "./codeBlock";

export function DetailContent({ r }: { r: MockReq }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <Row label="Request ID" value={<code>{r.requestId}</code>} />
      <Row label="Time" value={`${fmtTime(r.ts)} · ${fmtAgo(r.ts, Date.now())}`} />
      <Row label="Caller" value={r.caller} />
      <Row label="Client IP" value={<code>{r.clientIp}</code>} />
      <Row
        label="Status / Duration"
        value={
          <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
            <StatusPill code={r.status} />
            {r.degraded && <DegradedPill />}
            <span style={{ color: "var(--text-muted)" }}>·</span>
            <span style={{ fontVariantNumeric: "tabular-nums" }}>
              {fmtMs(r.durationMs)}
            </span>
            <span style={{ color: "var(--text-muted)" }}>·</span>
            <span style={{ color: "var(--text-muted)" }}>
              {fmtBytes(r.responseBytes)}
            </span>
          </span>
        }
      />
      {r.degraded && r.degradedReasons && r.degradedReasons.length > 0 && (
        <Row label="Degraded reason" value={r.degradedReasons.join(" · ")} />
      )}
      <SectionHeader title="Request" />
      <KvBlock entries={r.requestHeaders} />
      {r.requestBody && (
        <CodeBlock
          label="Body"
          code={r.requestBody}
          contentType={headerValue(r.requestHeaders, "content-type")}
        />
      )}
      <SectionHeader title="Response" />
      <KvBlock entries={r.responseHeaders} />
      {r.responseBody && (
        <CodeBlock
          label="Body"
          code={r.responseBody}
          contentType={headerValue(r.responseHeaders, "content-type")}
        />
      )}
    </div>
  );
}

export function InlineRequestDetail({
  req,
  onClose,
}: {
  req: MockReq;
  onClose: () => void;
}) {
  const curl = useMemo(() => buildCurl(req), [req]);
  return (
    <div
      role="region"
      aria-label={`Selected request detail: ${req.method} ${req.path}`}
      style={{
        marginTop: 12,
        padding: 12,
        border: "1px solid rgba(122,167,255,0.32)",
        borderRadius: 8,
        background:
          "linear-gradient(180deg, rgba(122,167,255,0.10), rgba(255,255,255,0.045))",
        boxShadow: "0 10px 28px rgba(0,0,0,0.24)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 10,
          marginBottom: 10,
        }}
      >
        <div style={{ minWidth: 0 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              marginBottom: 4,
              flexWrap: "wrap",
            }}
          >
            <MethodPill method={req.method} />
            <StatusPill code={req.status} />
            {req.degraded && <DegradedPill />}
            <span
              style={{
                color: latencyTone(req.durationMs),
                fontSize: 11,
                fontWeight: 700,
                fontVariantNumeric: "tabular-nums",
              }}
            >
              {fmtMs(req.durationMs)}
            </span>
            <span style={{ color: "var(--text-faint)", fontSize: 10 }}>
              {fmtTime(req.ts)}
            </span>
          </div>
          <code
            style={{
              display: "block",
              color: "var(--text-primary)",
              fontSize: 12,
              lineHeight: 1.4,
              whiteSpace: "normal",
              wordBreak: "break-all",
            }}
          >
            {req.path}
          </code>
        </div>
        <div style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
          <CopyActionButton
            value={curl}
            label="curl"
            title="Copy as curl (Authorization redacted)"
            iconSize={11}
            style={{ padding: "3px 7px" }}
          />
          <button
            type="button"
            className="glass-button"
            onClick={onClose}
            aria-label="Close selected request detail"
            title="Close detail"
            style={{
              padding: 4,
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <X size={12} />
          </button>
        </div>
      </div>
      <DetailContent r={req} />
    </div>
  );
}

export function Drawer({ req, onClose }: { req: MockReq; onClose: () => void }) {
  const curl = useMemo(() => buildCurl(req), [req]);
  return (
    <div
      role="dialog"
      aria-label={`Request detail: ${req.method} ${req.path}`}
      style={{
        position: "absolute",
        top: 0,
        right: 0,
        bottom: 0,
        width: 380,
        background: "rgba(8, 12, 24, 0.95)",
        borderLeft: "1px solid var(--border-weak)",
        padding: 14,
        overflowY: "auto",
        zIndex: 20,
        boxShadow: "-12px 0 32px rgba(0,0,0,0.45)",
        animation: "slideIn 180ms ease-out",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 10,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12 }}>
          <MethodPill method={req.method} />
          <StatusPill code={req.status} />
          {req.degraded && <DegradedPill />}
          <code style={{ fontSize: 11 }}>{req.path}</code>
        </div>
        <div style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
          <CopyActionButton
            value={curl}
            label="curl"
            title="Copy as curl (Authorization redacted)"
            iconSize={11}
            style={{ padding: "3px 7px" }}
          />
          <button
            type="button"
            className="glass-button"
            onClick={onClose}
            aria-label="Close request detail (Esc)"
            title="Close (Esc)"
            style={{
              padding: 4,
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <X size={12} />
          </button>
        </div>
      </div>
      <DetailContent r={req} />
    </div>
  );
}
