/**
 * Typed client for the read-only NCBI nuccore lookups exposed by
 * `api/routes/ncbi.py`. Used by the Sequence Detail page and (later) the
 * BLAST submit flow for "fetch by accession" preview.
 */
import { api } from "@/api/client";

export interface NuccoreSummary {
  accession: string;
  accession_version: string | null;
  title: string | null;
  organism: string | null;
  taxid: number | null;
  length: number | null;
  moltype: string | null;
  biomol: string | null;
  completeness: string | null;
  source_db: string | null;
  strand: string | null;
  topology: string | null;
  create_date: string | null;
  update_date: string | null;
  cached: boolean;
  source: "esummary" | "esummary_fallback";
}

export interface NuccoreFeatureInterval {
  start: number | null;
  stop: number | null;
  point: number | null;
  accession: string | null;
}

/**
 * A single GenBank feature qualifier. The backend returns qualifiers as an
 * ordered list of `{name, value}` pairs (NOT a map) so duplicate keys such as
 * repeated `db_xref` entries on the `source` feature are preserved.
 */
export interface NuccoreQualifier {
  name: string;
  value: string;
}

export interface NuccoreFeature {
  key: string | null;
  location: string | null;
  intervals: NuccoreFeatureInterval[];
  qualifiers: NuccoreQualifier[];
}

export interface NuccoreReference {
  title: string | null;
  journal: string | null;
  authors: string[];
  pubmed: string | null;
}

/**
 * A record-level DBLINK cross-reference (e.g. BioProject / BioSample /
 * Assembly / SRA), parsed from `GBSeq_xrefs`.
 */
export interface NuccoreGenBankXref {
  dbname: string;
  id: string;
}

export interface NuccoreGenBank {
  accession: string;
  accession_version: string | null;
  locus: string | null;
  definition: string | null;
  length: number | null;
  moltype: string | null;
  topology: string | null;
  strandedness: string | null;
  division: string | null;
  create_date: string | null;
  update_date: string | null;
  organism: string | null;
  /** Semicolon-delimited lineage string from `GBSeq_taxonomy` (root → genus). */
  taxonomy_lineage: string;
  source: string | null;
  comment: string | null;
  features: NuccoreFeature[];
  references: NuccoreReference[];
  xrefs: NuccoreGenBankXref[];
  cached: boolean;
  data_source: "ncbi_eutils" | string;
}

export function getNuccoreSummary(accession: string) {
  return api.get<NuccoreSummary>(
    `/ncbi/nuccore/${encodeURIComponent(accession)}`,
  );
}

export function getNuccoreGenBank(accession: string) {
  return api.get<NuccoreGenBank>(
    `/ncbi/nuccore/${encodeURIComponent(accession)}/genbank`,
  );
}

export interface NuccoreFastaOptions {
  seqStart?: number;
  seqStop?: number;
}

export function getNuccoreFastaPath(
  accession: string,
  { seqStart, seqStop }: NuccoreFastaOptions = {},
): string {
  const params = new URLSearchParams();
  if (seqStart != null) params.set("seq_start", String(seqStart));
  if (seqStop != null) params.set("seq_stop", String(seqStop));
  const qs = params.toString();
  return `/ncbi/nuccore/${encodeURIComponent(accession)}/fasta${qs ? `?${qs}` : ""}`;
}

export async function getNuccoreFasta(
  accession: string,
  options: NuccoreFastaOptions = {},
): Promise<string> {
  const result = await api.getText(getNuccoreFastaPath(accession, options));
  return result.text;
}
