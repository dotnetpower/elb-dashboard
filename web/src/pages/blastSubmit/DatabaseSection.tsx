import { AlertTriangle, Database } from "lucide-react";
import { Link } from "react-router-dom";

import { buildDatabasePath } from "@/pages/blastSubmit/helpers";
import { DB_DESCRIPTIONS } from "@/pages/blastSubmitModel";
import type { DatabaseSectionProps } from "@/pages/blastSubmit/types";
import { SectionHeader, Tip } from "@/pages/blastSubmit/ui";

export function DatabaseSection({
  form,
  set,
  programMeta,
  databases,
  dbWarning,
  dbMissingFromStorage,
  dbBaseName,
}: DatabaseSectionProps) {
  return (
    <section className="glass-card blast-section">
      <SectionHeader
        step={3}
        icon={<Database size={16} strokeWidth={1.5} />}
        title="Choose Search Set"
        subtitle="Select a BLAST database from your storage"
      />
      <label>
        <span className="glass-label">
          Database <Tip text="Select a BLAST database from your storage account." />
        </span>
        {databases && databases.length > 0 ? (
          <>
            <select
              className="glass-input"
              value={form.db}
              onChange={(event) => set("db", event.target.value)}
            >
              <option value="">— Select a database —</option>
              {databases.map((database) => {
                const info = DB_DESCRIPTIONS[database.name];
                const isCustom = database.source === "custom";
                const label = info
                  ? `${info.label} (${database.name}) — ${info.size}`
                  : isCustom
                    ? `${database.name} [Custom]`
                    : database.name;
                return (
                  <option key={database.name} value={buildDatabasePath(database)}>
                    {label}
                  </option>
                );
              })}
            </select>
            {!form.db && (
              <div className="blast-db-chips">
                <span className="muted" style={{ fontSize: 11 }}>
                  Suggested for {form.program} ({programMeta.dbType === "nucl" ? "nucleotide" : "protein"}):
                </span>
                {databases
                  .filter((database) => DB_DESCRIPTIONS[database.name]?.type === programMeta.dbType)
                  .slice(0, 4)
                  .map((database) => {
                    const info = DB_DESCRIPTIONS[database.name];
                    return (
                      <button
                        key={database.name}
                        className="blast-db-chip"
                        onClick={() => set("db", buildDatabasePath(database))}
                      >
                        <Database size={10} />
                        <span>{database.name}</span>
                        {info && <span className="blast-db-chip__size">{info.size}</span>}
                      </button>
                    );
                  })}
              </div>
            )}
          </>
        ) : (
          <input
            className="glass-input"
            value={form.db}
            onChange={(event) => set("db", event.target.value)}
            placeholder="blast-db/core_nt/core_nt"
            spellCheck={false}
          />
        )}
      </label>
      {form.db && databases && dbMissingFromStorage && (
        <div className="blast-warning-box">
          <AlertTriangle size={14} />
          This database doesn't appear to be downloaded yet. {dbBaseName && `(${dbBaseName}) `}
          <Link to="/" style={{ color: "var(--accent)" }}>
            Download it from the Dashboard
          </Link>
          .
        </div>
      )}
      {dbWarning && (
        <div className="blast-warning-box">
          <AlertTriangle size={14} />
          {dbWarning}
        </div>
      )}
    </section>
  );
}
