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
  /** NCBI record status ("live" / "replaced" / "suppressed" / "withdrawn" / "dead"). */
  status: string | null;
  /** Accession that supersedes this record, when `status` indicates replacement. */
  replaced_by: string | null;
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
  /** True when `value` was clipped by the backend and the full text lives on NCBI. */
  truncated?: boolean;
}

export interface NuccoreFeature {
  key: string | null;
  location: string | null;
  intervals: NuccoreFeatureInterval[];
  qualifiers: NuccoreQualifier[];
}

export interface NuccoreReference {
  /** Reference ordinal / position label from `GBReference_reference` (e.g. "1"). */
  reference: string | null;
  title: string | null;
  journal: string | null;
  authors: string[];
  /** Consortium author from `GBReference_consortium`, when present. */
  consortium: string | null;
  /** DOI from the `GBReference_xref` block, when present. */
  doi: string | null;
  /** Free-text `GBReference_remark` (GeneRIF / publication notes). */
  remark: string | null;
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
  /** Bare primary accession from `GBSeq_primary-accession`. */
  primary_accession: string | null;
  /** GI number extracted from `GBSeq_other-seqids`, when present. */
  gi: string | null;
  /** Raw seqid identifiers from `GBSeq_other-seqids`. */
  other_seqids: string[];
  /** Secondary accessions from `GBSeq_secondary-accessions`. */
  secondary_accessions: string[];
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
  /** Record-level keywords from `GBSeq_keywords` (empty when none). */
  keywords: string[];
  source: string | null;
  comment: string | null;
  /**
   * Names of record fields the backend clipped (e.g. "definition", "comment",
   * "taxonomy_lineage"). The UI flags these with a "view full record on NCBI"
   * affordance so the researcher never mistakes a clipped value for the whole.
   */
  truncated_fields?: string[];
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
