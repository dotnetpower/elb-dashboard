import { AlertTriangle, Check, CheckCircle2 } from "lucide-react";
import { Link } from "react-router-dom";

import type { PreFlightCheck, PreFlightResult } from "./usePreFlight";

export interface PreFlightResultPanelProps {
  result: PreFlightResult;
  onPickDb: (path: string) => void;
}

function CheckRow({ c }: { c: PreFlightCheck }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        fontSize: 11,
      }}
    >
      {c.status === "pass" ? (
        <CheckCircle2 size={11} style={{ color: "var(--success)" }} />
      ) : c.status === "fail" ? (
        <AlertTriangle
          size={11}
          style={{
            color:
              c.severity === "critical" ? "var(--danger)" : "var(--warning)",
          }}
        />
      ) : c.status === "warn" ? (
        <AlertTriangle
          size={11}
          style={{ color: "var(--warning)", opacity: 0.7 }}
        />
      ) : (
        <Check size={11} style={{ color: "var(--text-faint)" }} />
      )}
      <span
        style={{
          color:
            c.status === "pass" ? "var(--text-muted)" : "var(--text-primary)",
        }}
      >
        {c.title}
      </span>
      {c.detail && (
        <span className="muted" style={{ fontSize: 10 }}>
          — {c.detail}
        </span>
      )}
      {c.action && c.status === "fail" && (
        <span
          style={{
            fontSize: 10,
            color: "var(--accent)",
            marginLeft: "auto",
          }}
        >
          {c.action_type === "download_db" ? (
            <Link to="/" style={{ color: "var(--accent)" }}>
              {c.action} →
            </Link>
          ) : (
            c.action
          )}
        </span>
      )}
    </div>
  );
}

export function PreFlightResultPanel({
  result,
  onPickDb,
}: PreFlightResultPanelProps) {
  const dbCheck = result.checks.find(
    (c) => c.id === "blast_db" && c.status === "fail" && c.suggested_dbs,
  );
  return (
    <div
      style={{
        background: result.ready
          ? "rgba(115,191,105,0.06)"
          : "rgba(242,153,74,0.06)",
        border: `1px solid ${result.ready ? "rgba(115,191,105,0.2)" : "rgba(242,153,74,0.2)"}`,
        borderRadius: 8,
        padding: "12px 16px",
        marginBottom: 8,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          marginBottom: 8,
        }}
      >
        {result.ready ? (
          <CheckCircle2 size={14} style={{ color: "var(--success)" }} />
        ) : (
          <AlertTriangle size={14} style={{ color: "var(--warning)" }} />
        )}
        <span style={{ fontSize: 12, fontWeight: 600 }}>{result.summary}</span>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {result.checks.map((c) => (
          <CheckRow key={c.id} c={c} />
        ))}
      </div>
      {dbCheck && (
        <div
          style={{ marginTop: 8, fontSize: 11, color: "var(--text-muted)" }}
        >
          <span style={{ fontWeight: 600 }}>
            Suggested databases to download:{" "}
          </span>
          {dbCheck.suggested_dbs?.map((db, i) => (
            <span key={db}>
              {i > 0 && ", "}
              <button
                style={{
                  background: "none",
                  border: "none",
                  color: "var(--accent)",
                  cursor: "pointer",
                  fontSize: 11,
                  padding: 0,
                  textDecoration: "underline",
                }}
                onClick={() => onPickDb(`blast-db/${db}/${db}`)}
              >
                {db}
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
