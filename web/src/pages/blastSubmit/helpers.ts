import type { BlastDatabase } from "@/api/endpoints";
import { DB_CATALOG } from "@/components/cards/storageDbCatalog";
import { DB_DESCRIPTIONS, PROGRAMS, type FormState } from "@/pages/blastSubmitModel";
import { isBlastDbReady } from "@/utils/blastDbReady";

export function getDbBaseName(dbPath: string): string {
  return dbPath ? (dbPath.split("/").filter(Boolean).pop() ?? "") : "";
}

export function buildDatabasePath(db: BlastDatabase): string {
  const prefix = db.prefix ?? db.name;
  return `${db.container}/${prefix}/${db.name}`;
}

export function databaseExists(databases: BlastDatabase[], dbPath: string): boolean {
  const baseName = getDbBaseName(dbPath);
  return Boolean(baseName) && databases.some((db) => db.name === baseName);
}

// Name → molecule type lookup built once from the prepare-db catalogue. The
// curated `DB_DESCRIPTIONS` map wins because it is the same source the rest of
// the submit form trusts; the broader storage catalogue fills in the long tail.
const DB_CATALOG_TYPE_BY_NAME: Record<string, "nucl" | "prot"> = Object.fromEntries(
  DB_CATALOG.map((item) => [item.value, item.type]),
);

/**
 * Resolve a database's molecule type (nucleotide vs protein) from the known
 * catalogues. Returns `null` for unrecognised names (custom `makeblastdb`
 * builds, ad-hoc uploads) so callers treat them as "compatible with any
 * program" instead of guessing — we must never block a program because we
 * could not classify a custom DB.
 */
export function resolveDbMoleculeType(name: string): "nucl" | "prot" | null {
  return DB_DESCRIPTIONS[name]?.type ?? DB_CATALOG_TYPE_BY_NAME[name] ?? null;
}

/**
 * Which molecule types have at least one ready-to-search database downloaded.
 * Drives whether a program tab is selectable. `undefined` databases (list not
 * loaded yet, or manual-path mode) stay permissive — both types available — so
 * we never block the picker on a transient loading state. A ready DB whose type
 * we cannot classify also unlocks both types (we cannot prove it incompatible).
 */
export function deriveDbAvailabilityByType(
  databases: BlastDatabase[] | undefined,
): { nucl: boolean; prot: boolean } {
  if (!databases) return { nucl: true, prot: true };
  let nucl = false;
  let prot = false;
  let unknown = false;
  for (const db of databases) {
    if (!isBlastDbReady(db)) continue;
    const type = resolveDbMoleculeType(db.name);
    if (type === "nucl") nucl = true;
    else if (type === "prot") prot = true;
    else unknown = true;
  }
  return { nucl: nucl || unknown, prot: prot || unknown };
}

export type ProgramSwitchDecision =
  | { kind: "switch" }
  | { kind: "switch-db"; db: string }
  | { kind: "blocked"; molecule: "nucl" | "prot" };

/**
 * Decide what should happen to the selected database when the researcher picks
 * a new program. Mirrors the existing step-gating UX:
 *
 * - `switch` — the current DB is already compatible (same molecule type, ready)
 *   or its type is unknown, so keep it and just change the program.
 * - `switch-db` — the current DB is incompatible/empty but a ready DB of the
 *   program's molecule type exists, so overwrite the selection with it.
 * - `blocked` — no ready DB of the required molecule type is downloaded; the
 *   caller should keep the current program and tell the user to prepare one.
 *
 * `undefined` databases (loading / manual-path mode) always resolves to
 * `switch` so the picker stays usable.
 */
export function decideProgramSwitch(
  next: (typeof PROGRAMS)[0],
  currentDbPath: string,
  databases: BlastDatabase[] | undefined,
): ProgramSwitchDecision {
  if (!databases) return { kind: "switch" };
  const current = currentDbPath
    ? databases.find((db) => buildDatabasePath(db) === currentDbPath)
    : undefined;
  if (current && isBlastDbReady(current)) {
    const currentType = resolveDbMoleculeType(current.name);
    if (currentType === null || currentType === next.dbType) {
      return { kind: "switch" };
    }
  }
  const replacement = databases.find(
    (db) => isBlastDbReady(db) && resolveDbMoleculeType(db.name) === next.dbType,
  );
  if (replacement) return { kind: "switch-db", db: buildDatabasePath(replacement) };
  return { kind: "blocked", molecule: next.dbType };
}

export function getDatabaseWarning(
  form: FormState,
  programMeta: (typeof PROGRAMS)[0],
): string | null {
  const isNuclDb = form.db && /\b(nt|core_nt)\b/.test(form.db);
  const isProtDb = form.db && /\b(nr|swissprot|refseq_protein|pdb)\b/.test(form.db);
  const dbName = getDbBaseName(form.db);

  if (programMeta.dbType === "prot" && isNuclDb) {
    return `${form.program} expects a protein database, but "${dbName}" appears to be nucleotide.`;
  }
  if (programMeta.dbType === "nucl" && isProtDb) {
    return `${form.program} expects a nucleotide database, but "${dbName}" appears to be protein.`;
  }
  return null;
}

export function getSequenceStats(queryData: string) {
  const isFasta = queryData.trim().startsWith(">");
  const seqCount = queryData ? queryData.split("\n").filter((line) => line.startsWith(">")).length : 0;
  return {
    isFasta,
    seqCount,
    charCount: queryData.length,
  };
}
