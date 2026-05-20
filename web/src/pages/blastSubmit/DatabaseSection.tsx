import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Database, Download } from "lucide-react";
import { Link } from "react-router-dom";

import type { BlastDatabase } from "@/api/endpoints";
import { buildDatabasePath } from "@/pages/blastSubmit/helpers";
import { DB_DESCRIPTIONS } from "@/pages/blastSubmitModel";
import type { DatabaseSectionProps } from "@/pages/blastSubmit/types";
import { SectionHeader, Tip } from "@/pages/blastSubmit/ui";
import { DB_CATALOG, type BlastDbCatalogItem } from "@/components/cards/storageDbCatalog";

type SearchSetCategory = "standard" | "rna" | "genomic" | "custom";

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

function deriveCategoryFromForm(
  databases: BlastDatabase[] | undefined,
  formDb: string,
): SearchSetCategory {
  if (!databases || !formDb) return "standard";
  const current = databases.find((database) => buildDatabasePath(database) === formDb);
  return current ? databaseCategory(current) : "standard";
}

// Entries from DB_CATALOG that NCBI surfaces in this tab but do NOT exist in
// the user's blast-db storage yet. We render them as disabled options with a
// "— Not downloaded" suffix so the operator can see the full reference list
// (matching NCBI Web BLAST) and follow the helper link to add them.
function notDownloadedCatalogue(
  category: SearchSetCategory,
  downloaded: BlastDatabase[] | undefined,
  programType: "nucl" | "prot",
): BlastDbCatalogItem[] {
  if (category === "custom") return [];
  const downloadedNames = new Set((downloaded ?? []).map((database) => database.name));
  return DB_CATALOG.filter((item) => {
    if (item.type !== programType) return false;
    if (downloadedNames.has(item.value)) return false;
    return databaseCategoryByName(item.value) === category;
  });
}

export function DatabaseSection({
  form,
  set,
  programMeta,
  databases,
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
  // is dropped if it does not belong to the new category. Without this the
  // dropdown would prepend an off-category "current" entry, masking the
  // category swap and leaving the impression that the database is locked.
  const handleCategoryChange = (next: SearchSetCategory) => {
    if (next === category) return;
    setCategory(next);
    if (!databases || !form.db) return;
    const current = databases.find((database) => buildDatabasePath(database) === form.db);
    if (current && databaseCategory(current) !== next) {
      set("db", "");
    }
  };

  const visibleDatabases = useMemo(() => {
    if (!databases) return [];
    return databases.filter((database) => databaseCategory(database) === category);
  }, [category, databases]);

  // NCBI Web BLAST surfaces a much larger reference catalogue than what we
  // typically pre-stage in storage. Show the rest of the NCBI standard
  // catalogue as disabled options so the operator can discover them, and
  // route them to the Storage card to actually add them.
  const catalogueGap = useMemo(
    () => notDownloadedCatalogue(category, databases, programMeta.dbType),
    [category, databases, programMeta.dbType],
  );

  return (
    <section className="glass-card blast-section">
      <SectionHeader
        step={3}
        icon={<Database size={16} strokeWidth={1.5} />}
        title="Choose Search Set"
        subtitle="Select a BLAST database from your storage"
      />
      <div>
        <span className="glass-label">
          Database <Tip text="Select a BLAST database from your storage account." />
        </span>
        {databases && databases.length > 0 ? (
          <>
            <div className="blast-search-set-tabs" role="radiogroup" aria-label="Database category">
              {SEARCH_SET_CATEGORIES.map((item) => {
                const downloadedCount = databases.filter(
                  (database) => databaseCategory(database) === item.value,
                ).length;
                // The catalogue gap lets the tab remain selectable even when
                // nothing is downloaded yet, so the operator can see the NCBI
                // reference list and add a database.
                const catalogueCount = notDownloadedCatalogue(
                  item.value,
                  databases,
                  programMeta.dbType,
                ).length;
                const totalCount = downloadedCount + catalogueCount;
                const isActive = category === item.value;
                const tabTitle =
                  totalCount === 0
                    ? "No databases in this category"
                    : downloadedCount === 0
                      ? `${catalogueCount} NCBI ${item.label.toLowerCase()} available to add — none downloaded yet`
                      : `${downloadedCount} downloaded · ${catalogueCount} more available from NCBI`;
                return (
                  <button
                    key={item.value}
                    type="button"
                    className={`blast-search-set-tab${isActive ? " blast-search-set-tab--active" : ""}`}
                    aria-checked={isActive}
                    role="radio"
                    disabled={totalCount === 0}
                    onClick={() => handleCategoryChange(item.value)}
                    title={tabTitle}
                  >
                    <span className="blast-search-set-tab__radio" aria-hidden="true" />
                    <span className="blast-search-set-tab__label">{item.label}</span>
                    <small>
                      {downloadedCount}
                      {catalogueCount > 0 && (
                        <span className="blast-search-set-tab__hint"> /+{catalogueCount}</span>
                      )}
                    </small>
                  </button>
                );
              })}
            </div>
            <select
              className="glass-input"
              value={form.db}
              onChange={(event) => set("db", event.target.value)}
            >
              <option value="">— Select a database —</option>
              {visibleDatabases.length > 0 && (
                <optgroup label="In storage (ready)">
                  {visibleDatabases.map((database) => {
                    const info = DB_DESCRIPTIONS[database.name];
                    const isCustom = database.source === "custom";
                    const warmInfo = warmDbs?.get(database.name);
                    const warmLabel = warmInfo
                      ? `Warm ${warmInfo.nodes_ready}/${warmInfo.total_jobs}`
                      : warmupKnown
                        ? "Not warm"
                        : null;
                    const label = info
                      ? `${info.label} (${database.name}) — ${info.size}`
                      : isCustom
                        ? `${database.name} [Custom]`
                        : database.name;
                    return (
                      <option key={database.name} value={buildDatabasePath(database)}>
                        {warmLabel ? `${label} — ${warmLabel}` : label}
                      </option>
                    );
                  })}
                </optgroup>
              )}
              {catalogueGap.length > 0 && (
                <optgroup label="Available from NCBI (not downloaded yet)">
                  {catalogueGap.map((item) => (
                    // Disabled so the operator can see what's selectable on NCBI
                    // Web BLAST but cannot accidentally submit a job against a
                    // database that does not yet live in our blast-db container.
                    // The "Add database" link below the dropdown routes them to
                    // the Storage card to trigger an actual download.
                    <option
                      key={`catalog-${item.value}`}
                      value=""
                      disabled
                    >
                      {`${item.label} (${item.value}) — ${item.size} — Not downloaded`}
                    </option>
                  ))}
                </optgroup>
              )}
            </select>
            {catalogueGap.length > 0 && (
              <div className="blast-db-add-hint">
                <Download size={12} strokeWidth={1.5} />
                <span>
                  {catalogueGap.length} more NCBI {programMeta.dbType === "nucl" ? "nucleotide" : "protein"}{" "}
                  {catalogueGap.length === 1 ? "database is" : "databases are"} listed above as reference.{" "}
                  <Link to="/" style={{ color: "var(--accent)" }}>
                    Add one from the Dashboard Storage card
                  </Link>{" "}
                  to make it submittable.
                </span>
              </div>
            )}
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
          <input
            className="glass-input"
            value={form.db}
            onChange={(event) => set("db", event.target.value)}
            placeholder="blast-db/core_nt/core_nt"
            spellCheck={false}
          />
        )}
      </div>
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
