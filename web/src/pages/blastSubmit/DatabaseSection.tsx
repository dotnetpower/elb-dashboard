import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Database } from "lucide-react";
import { Link } from "react-router-dom";

import type { BlastDatabase } from "@/api/endpoints";
import { buildDatabasePath } from "@/pages/blastSubmit/helpers";
import { DB_DESCRIPTIONS } from "@/pages/blastSubmitModel";
import type { DatabaseSectionProps } from "@/pages/blastSubmit/types";
import { SectionHeader, Tip } from "@/pages/blastSubmit/ui";
import { formatBytes } from "@/components/cards/storageDbCatalog";

export type SearchSetCategory = "standard" | "rna" | "genomic" | "custom";

const SEARCH_SET_CATEGORIES: Array<{ value: SearchSetCategory; label: string }> = [
  { value: "standard", label: "Standard databases" },
  { value: "rna", label: "rRNA/ITS databases" },
  { value: "genomic", label: "Genomic + transcript" },
  { value: "custom", label: "Custom databases" },
];

// Categorises a database (either a downloaded BlastDatabase or a catalogue
// entry) into one of the four NCBI-style tabs. The order matters — the more
// specific rRNA/ITS pattern must win over the generic "refseq" hit so that
// e.g. "16S_ribosomal_RNA" stays in the rRNA tab rather than spilling into
// the Standard tab.
function databaseCategoryByName(name: string, source?: string): SearchSetCategory {
  if (source === "custom") return "custom";
  if (/16s|18s|28s|rrna|its/i.test(name)) return "rna";
  // Genomic / transcript bucket: NCBI groups assembled genomes (refseq_genomes,
  // refseq_reference_genomes, wgs) and transcript assemblies (tsa, est) here.
  if (/^(wgs|tsa|est|refseq_(reference_)?genomes)$/i.test(name)) return "genomic";
  return "standard";
}

function databaseCategory(database: BlastDatabase): SearchSetCategory {
  return databaseCategoryByName(database.name, database.source ?? undefined);
}

export function firstDatabasePathForCategory(
  databases: BlastDatabase[] | undefined,
  category: SearchSetCategory,
): string {
  const first = databases?.find((database) => databaseCategory(database) === category);
  return first ? buildDatabasePath(first) : "";
}

function deriveCategoryFromForm(
  databases: BlastDatabase[] | undefined,
  formDb: string,
): SearchSetCategory {
  if (!databases || !formDb) return "standard";
  const current = databases.find((database) => buildDatabasePath(database) === formDb);
  return current ? databaseCategory(current) : "standard";
}

function databaseDisplayName(database: BlastDatabase): string {
  return (
    DB_DESCRIPTIONS[database.name]?.label ??
    (database.source === "custom" ? "Custom database" : "Downloaded database")
  );
}

function databaseSizeLabel(database: BlastDatabase): string {
  return (
    DB_DESCRIPTIONS[database.name]?.size ??
    (database.total_bytes ? formatBytes(database.total_bytes) : "—")
  );
}

function databaseTypeLabel(
  database: BlastDatabase,
  fallbackType: "nucl" | "prot",
): "nucl" | "prot" {
  return DB_DESCRIPTIONS[database.name]?.type ?? fallbackType;
}

function databaseNameFromPath(path: string, fallbackName: string): string {
  if (fallbackName) return fallbackName;
  const parts = path.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? "Database path";
}

export function DatabaseSection({
  form,
  set,
  programMeta,
  databases,
  dbLoading = false,
  warmDbs,
  warmupKnown,
  dbWarning,
  dbMissingFromStorage,
  dbBaseName,
}: DatabaseSectionProps) {
  const [category, setCategory] = useState<SearchSetCategory>(() =>
    deriveCategoryFromForm(databases, form.db),
  );

  // When the database list finishes loading (or the form draft restores a DB
  // belonging to a different category), reflect that in the active tab so the
  // user sees the radio they actually have selected. The effect only re-syncs
  // when `databases`/`form.db` change — manual category clicks are preserved
  // because they do not modify those dependencies.
  useEffect(() => {
    if (!databases || !form.db) return;
    const implied = deriveCategoryFromForm(databases, form.db);
    setCategory((prev) => (prev === implied ? prev : implied));
  }, [databases, form.db]);

  // Clicking a different category radio should follow NCBI behaviour: the
  // dropdown switches to that category's options and the previous selection
  // is replaced by the first downloaded database in the new category. Without
  // this the next Query step can stay disabled even though the category has
  // usable databases.
  const handleCategoryChange = (next: SearchSetCategory) => {
    if (!databases) {
      setCategory(next);
      return;
    }
    const current = form.db
      ? databases.find((database) => buildDatabasePath(database) === form.db)
      : undefined;
    if (next === category && current && databaseCategory(current) === next) return;
    setCategory(next);
    if (current && databaseCategory(current) === next) return;
    set("db", firstDatabasePathForCategory(databases, next));
  };

  const visibleDatabases = useMemo(() => {
    if (!databases) return [];
    return databases.filter((database) => databaseCategory(database) === category);
  }, [category, databases]);

  const fallbackDatabaseName = databaseNameFromPath(form.db, dbBaseName);
  const fallbackDatabaseSize = DB_DESCRIPTIONS[fallbackDatabaseName]?.size ?? "—";

  return (
    <section
      className={`glass-card blast-section bsl-input${form.db ? " bsl-done" : ""}`}
    >
      <SectionHeader
        step={2}
        icon={<Database size={16} strokeWidth={1.5} />}
        title="Choose Search Set"
        subtitle="Select a BLAST database from your storage"
      />
      <div>
        <span className="glass-label">
          Database <Tip text="Select a BLAST database from your storage account." />
        </span>
        {dbLoading ? (
          <DatabaseLoadingSkeleton />
        ) : databases && databases.length > 0 ? (
          <>
            <div
              className="blast-search-set-tabs"
              role="radiogroup"
              aria-label="Database category"
            >
              {SEARCH_SET_CATEGORIES.map((item) => {
                const downloadedCount = databases.filter(
                  (database) => databaseCategory(database) === item.value,
                ).length;
                const isActive = category === item.value;
                const tabTitle =
                  downloadedCount === 0
                    ? "No databases in this category"
                    : `${downloadedCount} downloaded`;
                return (
                  <button
                    key={item.value}
                    type="button"
                    className={`blast-search-set-tab${isActive ? " blast-search-set-tab--active" : ""}`}
                    aria-checked={isActive}
                    role="radio"
                    disabled={downloadedCount === 0}
                    onClick={() => handleCategoryChange(item.value)}
                    title={tabTitle}
                  >
                    <span className="blast-search-set-tab__radio" aria-hidden="true" />
                    <span className="blast-search-set-tab__label">{item.label}</span>
                    <small>{downloadedCount}</small>
                  </button>
                );
              })}
            </div>
            <div className="blast-db-table" role="radiogroup" aria-label="Database">
              <div className="blast-db-table__head" aria-hidden="true">
                <span>Database</span>
                <span>Type</span>
                <span>Size</span>
                <span>Status</span>
              </div>
              {visibleDatabases.length > 0 ? (
                visibleDatabases.map((database) => {
                  const path = buildDatabasePath(database);
                  const isSelected = form.db === path;
                  const warmInfo = warmDbs?.get(database.name);
                  const typeLabel = databaseTypeLabel(database, programMeta.dbType);
                  const statusLabel = isSelected
                    ? warmInfo
                      ? "Selected · warmed"
                      : "Selected · ready"
                    : warmInfo
                      ? `Warmed ${warmInfo.nodes_ready}/${warmInfo.total_jobs}`
                      : warmupKnown
                        ? "Downloaded"
                        : "Ready";

                  return (
                    <button
                      key={path}
                      type="button"
                      className={`blast-db-table__row${isSelected ? " blast-db-table__row--selected" : ""}`}
                      role="radio"
                      aria-checked={isSelected}
                      onClick={() => set("db", path)}
                    >
                      <span className="blast-db-table__database">
                        <span className="blast-db-table__radio" aria-hidden="true" />
                        <span className="blast-db-table__name-wrap">
                          <span className="blast-db-table__name">{database.name}</span>
                          <span className="blast-db-table__desc">
                            {databaseDisplayName(database)}
                          </span>
                        </span>
                      </span>
                      <span className="blast-db-table__pill blast-db-table__pill--type">
                        {typeLabel}
                      </span>
                      <span className="blast-db-table__size">
                        {databaseSizeLabel(database)}
                      </span>
                      <span
                        className={`blast-db-table__pill${isSelected ? " blast-db-table__pill--selected" : ""}`}
                      >
                        {statusLabel}
                      </span>
                    </button>
                  );
                })
              ) : (
                <div className="blast-db-table__empty">
                  No downloaded databases in this category.
                </div>
              )}
            </div>
            {!form.db && (
              <div className="blast-db-chips">
                <span className="muted" style={{ fontSize: 11 }}>
                  Suggested for {form.program} (
                  {programMeta.dbType === "nucl" ? "nucleotide" : "protein"}):
                </span>
                {databases
                  .filter(
                    (database) =>
                      DB_DESCRIPTIONS[database.name]?.type === programMeta.dbType,
                  )
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
                        {warmDbs?.has(database.name) && (
                          <span className="blast-db-chip__size">Warm</span>
                        )}
                      </button>
                    );
                  })}
              </div>
            )}
          </>
        ) : (
          <>
            <div
              className="blast-db-table"
              role="group"
              aria-label="Selected database path"
            >
              <div className="blast-db-table__head" aria-hidden="true">
                <span>Database</span>
                <span>Type</span>
                <span>Size</span>
                <span>Status</span>
              </div>
              {form.db ? (
                <div className="blast-db-table__row blast-db-table__row--selected">
                  <span className="blast-db-table__database">
                    <span className="blast-db-table__radio" aria-hidden="true" />
                    <span className="blast-db-table__name-wrap">
                      <span className="blast-db-table__name">{fallbackDatabaseName}</span>
                      <span className="blast-db-table__desc">Storage path</span>
                    </span>
                  </span>
                  <span className="blast-db-table__pill blast-db-table__pill--type">
                    {programMeta.dbType}
                  </span>
                  <span className="blast-db-table__size">{fallbackDatabaseSize}</span>
                  <span className="blast-db-table__pill blast-db-table__pill--selected">
                    Selected · manual
                  </span>
                </div>
              ) : (
                <div className="blast-db-table__empty">
                  Enter a database path to select a search set.
                </div>
              )}
            </div>
            {!form.db && (
              <input
                className="glass-input blast-db-manual-input"
                value={form.db}
                onChange={(event) => set("db", event.target.value)}
                placeholder="blast-db/core_nt/core_nt"
                spellCheck={false}
                aria-label="Database path"
              />
            )}
          </>
        )}
      </div>
      {form.db && databases && dbMissingFromStorage && (
        <div className="blast-warning-box">
          <AlertTriangle size={14} />
          This database doesn't appear to be downloaded yet.{" "}
          {dbBaseName && `(${dbBaseName}) `}
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

function DatabaseLoadingSkeleton() {
  return (
    <div className="blast-db-table" role="status" aria-label="Loading databases">
      <div className="blast-db-table__head" aria-hidden="true">
        <span>Database</span>
        <span>Type</span>
        <span>Size</span>
        <span>Status</span>
      </div>
      {Array.from({ length: 3 }, (_, index) => (
        <div key={index} className="blast-db-table__row" aria-hidden="true">
          <span className="blast-db-table__database">
            <span className="blast-db-table__radio" />
            <span className="blast-db-table__name-wrap">
              <SkeletonLine width={index === 0 ? "96px" : "132px"} />
              <SkeletonLine width={index === 1 ? "108px" : "86px"} />
            </span>
          </span>
          <SkeletonLine width="42px" />
          <SkeletonLine width="64px" />
          <SkeletonLine width="84px" />
        </div>
      ))}
    </div>
  );
}

function SkeletonLine({ width }: { width: string }) {
  return (
    <span
      className="skeleton"
      style={{ display: "block", width, height: 10, borderRadius: 999 }}
    />
  );
}
