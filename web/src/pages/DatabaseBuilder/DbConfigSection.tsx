import { Dna, FlaskConical, Settings } from "lucide-react";

import { SectionHeader } from "./SectionHeader";
import type { DatabaseBuilderState } from "./useDatabaseBuilderState";

export interface DbConfigSectionProps {
  dbName: DatabaseBuilderState["dbName"];
  setDbName: DatabaseBuilderState["setDbName"];
  dbType: DatabaseBuilderState["dbType"];
  setDbType: DatabaseBuilderState["setDbType"];
  title: DatabaseBuilderState["title"];
  setTitle: DatabaseBuilderState["setTitle"];
  isValidDbName: DatabaseBuilderState["isValidDbName"];
  nameClash: DatabaseBuilderState["nameClash"];
}

export function DbConfigSection({
  dbName,
  setDbName,
  dbType,
  setDbType,
  title,
  setTitle,
  isValidDbName,
  nameClash,
}: DbConfigSectionProps) {
  return (
    <section className="glass-card blast-section">
      <SectionHeader
        step={1}
        icon={<Settings size={16} strokeWidth={1.5} />}
        title="Database Configuration"
        subtitle="Name, sequence type, and human-readable title"
      />

      <div className="db-builder-grid">
        <div className="form-row">
          <label className="form-label" htmlFor="db-name">
            Database name *
          </label>
          <input
            id="db-name"
            type="text"
            className="form-input"
            placeholder="e.g. my_pathogen_db"
            value={dbName}
            onChange={(e) => setDbName(e.target.value.replace(/[^a-zA-Z0-9_-]/g, ""))}
            maxLength={50}
          />
          {dbName && !isValidDbName && (
            <span className="form-hint" style={{ color: "var(--danger)" }}>
              Only letters, digits, _ and - allowed (1-50 chars)
            </span>
          )}
          {nameClash && (
            <span className="form-hint" style={{ color: "var(--warning)" }}>
              A database with this name already exists — it will be overwritten on
              rebuild.
            </span>
          )}
        </div>

        <div className="form-row">
          <label className="form-label">Sequence type *</label>
          <div className="blast-program-tabs">
            <button
              type="button"
              onClick={() => setDbType("nucl")}
              className={`blast-program-tab${dbType === "nucl" ? " blast-program-tab--active" : ""}`}
            >
              <span className="blast-program-tab__name">
                <Dna size={13} style={{ verticalAlign: "-2px", marginRight: 4 }} />
                Nucleotide
              </span>
              <span className="blast-program-tab__desc">DNA / RNA · -dbtype nucl</span>
            </button>
            <button
              type="button"
              onClick={() => setDbType("prot")}
              className={`blast-program-tab${dbType === "prot" ? " blast-program-tab--active" : ""}`}
            >
              <span className="blast-program-tab__name">
                <FlaskConical
                  size={13}
                  style={{ verticalAlign: "-2px", marginRight: 4 }}
                />
                Protein
              </span>
              <span className="blast-program-tab__desc">
                Amino acids · -dbtype prot
              </span>
            </button>
          </div>
        </div>

        <div className="form-row" style={{ gridColumn: "1 / -1" }}>
          <label className="form-label" htmlFor="db-title">
            Title (optional)
          </label>
          <input
            id="db-title"
            type="text"
            className="form-input"
            placeholder="Human-readable description shown in BLAST results"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            maxLength={200}
          />
        </div>
      </div>
    </section>
  );
}
