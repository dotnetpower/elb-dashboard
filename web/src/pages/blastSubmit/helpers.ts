import type { BlastDatabase } from "@/api/endpoints";
import type { FormState, PROGRAMS } from "@/pages/blastSubmitModel";

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
