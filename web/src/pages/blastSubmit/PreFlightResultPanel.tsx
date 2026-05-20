import { AlertTriangle, Check, CheckCircle2 } from "lucide-react";
import { Link } from "react-router-dom";

import type { PreFlightCheck, PreFlightResult } from "./usePreFlight";

export interface PreFlightResultPanelProps {
  result: PreFlightResult;
  onPickDb: (path: string) => void;
}

function CheckRow({ c }: { c: PreFlightCheck }) {
  const statusClass = `blast-preflight__row--${c.status}`;
  return (
    <div className={`blast-preflight__row ${statusClass}`}>
      {c.status === "pass" ? (
        <CheckCircle2 size={12} className="blast-preflight__icon" />
      ) : c.status === "fail" ? (
        <AlertTriangle
          size={12}
          className={`blast-preflight__icon${
            c.severity === "critical" ? " blast-preflight__icon--critical" : ""
          }`}
        />
      ) : c.status === "warn" ? (
        <AlertTriangle size={12} className="blast-preflight__icon" />
      ) : (
        <Check size={12} className="blast-preflight__icon" />
      )}
      <span className="blast-preflight__row-title">{c.title}</span>
      {c.detail && <span className="blast-preflight__row-detail">— {c.detail}</span>}
      {c.action && c.status === "fail" && (
        <span className="blast-preflight__row-action">
          {c.action_type === "download_db" ? <Link to="/">{c.action} →</Link> : c.action}
        </span>
      )}
    </div>
  );
}

export function PreFlightResultPanel({ result, onPickDb }: PreFlightResultPanelProps) {
  const dbCheck = result.checks.find(
    (c) => c.id === "blast_db" && c.status === "fail" && c.suggested_dbs,
  );
  return (
    <div
      className={`blast-preflight${
        result.ready ? " blast-preflight--ready" : " blast-preflight--blocked"
      }`}
    >
      <div className="blast-preflight__head">
        {result.ready ? (
          <CheckCircle2 size={15} className="blast-preflight__head-icon" />
        ) : (
          <AlertTriangle size={15} className="blast-preflight__head-icon" />
        )}
        <span>{result.summary}</span>
      </div>
      <div className="blast-preflight__checks">
        {result.checks.map((c) => (
          <CheckRow key={c.id} c={c} />
        ))}
      </div>
      {dbCheck && (
        <div className="blast-preflight__suggestions">
          <span>Suggested databases to download: </span>
          {dbCheck.suggested_dbs?.map((db, i) => (
            <span key={db}>
              {i > 0 && ", "}
              <button
                className="blast-preflight__suggestion-btn"
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
