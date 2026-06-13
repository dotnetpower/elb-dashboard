/**
 * Sidecar HTTP request inspector — small presentational atoms.
 *
 * Status/method/degraded pills, key-value blocks, table cells, the live
 * indicator, count chips, search bar, legend dots, and the card header.
 * Pure presentation; no data fetching, no local persistence.
 */

import { Search, X } from "lucide-react";
import { DEGRADED_BG, DEGRADED_COLOR, DEGRADED_RING, methodTone, statusTone } from "./format";

export function StatusPill({ code }: { code: number }) {
  const t = statusTone(code);
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding: "1px 6px",
        borderRadius: 4,
        fontSize: 10,
        fontWeight: 600,
        background: t.bg,
        color: t.fg,
        border: "1px solid " + t.ring,
        fontVariantNumeric: "tabular-nums",
      }}
    >
      {code}
    </span>
  );
}

export function DegradedPill() {
  return (
    <span
      title="HTTP request succeeded, but the response reports a degraded domain state"
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding: "1px 6px",
        borderRadius: 4,
        fontSize: 10,
        fontWeight: 700,
        color: DEGRADED_COLOR,
        background: DEGRADED_BG,
        border: `1px solid ${DEGRADED_RING}`,
      }}
    >
      Degraded
    </span>
  );
}

export function MethodPill({ method }: { method: string }) {
  return (
    <span
      style={{
        display: "inline-block",
        minWidth: 44,
        padding: "1px 5px",
        textAlign: "center",
        fontSize: 10,
        fontWeight: 700,
        letterSpacing: "0.04em",
        color: methodTone(method),
        background: "rgba(255,255,255,0.04)",
        border: "1px solid var(--border-weak)",
        borderRadius: 3,
        fontVariantNumeric: "tabular-nums",
      }}
    >
      {method}
    </span>
  );
}

export function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 12, alignItems: "baseline" }}>
      <span style={{ width: 110, color: "var(--text-muted)", fontSize: 11 }}>
        {label}
      </span>
      <span style={{ fontSize: 12, color: "var(--text-primary)" }}>{value}</span>
    </div>
  );
}

export function SectionHeader({ title }: { title: string }) {
  return (
    <div
      style={{
        marginTop: 8,
        fontSize: 10,
        textTransform: "uppercase",
        letterSpacing: "0.08em",
        color: "var(--text-muted)",
        borderBottom: "1px solid var(--border-weak)",
        paddingBottom: 4,
      }}
    >
      {title}
    </div>
  );
}

export function KvBlock({ entries }: { entries: Record<string, string> }) {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "auto 1fr",
        gap: "3px 10px",
        fontSize: 11,
        fontFamily: "var(--mono)",
      }}
    >
      {Object.entries(entries).map(([k, v]) => (
        <div key={k} style={{ display: "contents" }}>
          <span style={{ color: "var(--text-muted)" }}>{k}:</span>
          <span style={{ color: "var(--text-primary)", wordBreak: "break-all" }}>
            {v}
          </span>
        </div>
      ))}
    </div>
  );
}

export function Th({ children, align }: { children?: React.ReactNode; align?: "right" }) {
  return (
    <th
      style={{
        padding: "6px 10px",
        fontSize: 10,
        fontWeight: 500,
        textTransform: "uppercase",
        letterSpacing: "0.05em",
        textAlign: align ?? "left",
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </th>
  );
}

export function Td({ children, align }: { children?: React.ReactNode; align?: "right" }) {
  return (
    <td
      style={{
        padding: "6px 10px",
        whiteSpace: "nowrap",
        textAlign: align ?? "left",
        color: "var(--text-primary)",
      }}
    >
      {children}
    </td>
  );
}

export function LegendDot({
  color,
  label,
  shape = "circle",
}: {
  color: string;
  label: string;
  shape?: "circle" | "triangle";
}) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
      {shape === "triangle" ? (
        <span
          style={{
            width: 0,
            height: 0,
            borderLeft: "5px solid transparent",
            borderRight: "5px solid transparent",
            borderBottom: `9px solid ${color}`,
            display: "inline-block",
          }}
        />
      ) : (
        <span
          style={{
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: color,
            display: "inline-block",
          }}
        />
      )}
      {label}
    </span>
  );
}

export function LiveIndicator({ paused }: { paused: boolean }) {
  return (
    <span
      role="status"
      aria-live="polite"
      title={
        paused
          ? "Stream paused — click Resume to continue"
          : "Live — capturing every request through the api sidecar"
      }
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        padding: "2px 7px",
        background: "rgba(255,255,255,0.04)",
        border: "1px solid var(--border-weak)",
        borderRadius: 4,
        fontSize: 10,
        fontWeight: 600,
        letterSpacing: "0.06em",
        color: paused ? "var(--warning)" : "var(--success)",
      }}
    >
      <span
        style={{
          width: 7,
          height: 7,
          borderRadius: "50%",
          background: paused ? "var(--warning)" : "var(--success)",
          boxShadow: paused ? "none" : "0 0 0 0 rgba(106,214,163,0.7)",
          animation: paused ? "none" : "livePulse 1.6s infinite",
        }}
      />
      {paused ? "PAUSED" : "LIVE"}
    </span>
  );
}

export function CountChips({
  counts,
}: {
  counts: {
    ok: number;
    redirect: number;
    client: number;
    server: number;
    degraded: number;
  };
}) {
  const items: { label: string; n: number; color: string }[] = [
    { label: "ok", n: counts.ok, color: statusTone(200).fg },
    { label: "3xx", n: counts.redirect, color: statusTone(304).fg },
    { label: "4xx", n: counts.client, color: statusTone(404).fg },
    { label: "5xx", n: counts.server, color: statusTone(500).fg },
    { label: "degraded", n: counts.degraded, color: DEGRADED_COLOR },
  ];
  return (
    <div
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding: "2px 6px",
        background: "rgba(255,255,255,0.04)",
        border: "1px solid var(--border-weak)",
        borderRadius: 4,
        fontSize: 10,
        fontVariantNumeric: "tabular-nums",
      }}
      title="2xx · 3xx · 4xx · 5xx · degraded counts in current window"
    >
      {items.map((it, i) => (
        <span
          key={it.label}
          style={{ display: "inline-flex", alignItems: "center", gap: 3 }}
        >
          {i > 0 && (
            <span style={{ color: "var(--text-faint, var(--text-muted))" }}>·</span>
          )}
          {it.label === "degraded" ? (
            <span
              style={{
                width: 0,
                height: 0,
                borderLeft: "4px solid transparent",
                borderRight: "4px solid transparent",
                borderBottom: `7px solid ${it.color}`,
                display: "inline-block",
              }}
            />
          ) : (
            <span
              style={{
                width: 6,
                height: 6,
                borderRadius: "50%",
                background: it.color,
                display: "inline-block",
              }}
            />
          )}
          <span style={{ color: "var(--text-primary)", fontWeight: 600 }}>{it.n}</span>
          <span style={{ color: "var(--text-muted)" }}>{it.label}</span>
        </span>
      ))}
    </div>
  );
}

export function SearchBar({
  value,
  onChange,
  total,
  shown,
}: {
  value: string;
  onChange: (v: string) => void;
  total: number;
  shown: number;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        margin: "0 0 8px 0",
        padding: "0 2px",
      }}
    >
      <div
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          padding: "4px 8px",
          flex: 1,
          background: "rgba(255,255,255,0.04)",
          border: "1px solid var(--border-weak)",
          borderRadius: 6,
        }}
      >
        <Search size={12} style={{ color: "var(--text-muted)" }} />
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="Filter by path, caller, request_id, status code…"
          aria-label="Filter requests"
          style={{
            flex: 1,
            background: "transparent",
            border: "none",
            outline: "none",
            color: "var(--text-primary)",
            fontSize: 11,
            fontFamily: "inherit",
          }}
        />
        {value && (
          <button
            type="button"
            onClick={() => onChange("")}
            aria-label="Clear filter"
            title="Clear"
            style={{
              background: "transparent",
              border: "none",
              color: "var(--text-muted)",
              cursor: "pointer",
              padding: 0,
              display: "inline-flex",
            }}
          >
            <X size={11} />
          </button>
        )}
      </div>
      <span
        style={{
          fontSize: 10,
          color: "var(--text-muted)",
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {shown === total ? `${total} requests` : `${shown} of ${total}`}
      </span>
    </div>
  );
}

export function Header({
  eyebrow,
  title,
  blurb,
  right,
}: {
  eyebrow: string;
  title: string;
  blurb?: string;
  right?: React.ReactNode;
}) {
  return (
    <div
      style={{
        display: "flex",
        justifyContent: "space-between",
        alignItems: "flex-start",
        gap: 12,
        marginBottom: 4,
      }}
    >
      <div>
        <div
          style={{
            fontSize: 10,
            textTransform: "uppercase",
            letterSpacing: "0.08em",
            color: "var(--accent)",
            fontWeight: 600,
            marginBottom: 2,
          }}
        >
          {eyebrow}
        </div>
        <h3 style={{ margin: 0, fontSize: 15, fontWeight: 600 }}>{title}</h3>
        {blurb && (
          <p
            style={{
              margin: "2px 0 0",
              fontSize: 11,
              color: "var(--text-muted)",
              maxWidth: 720,
              lineHeight: 1.5,
            }}
          >
            {blurb}
          </p>
        )}
      </div>
      {right}
    </div>
  );
}
