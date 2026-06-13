/**
 * NCBI nuccore record derivation helpers for the Sequence Detail page.
 *
 * Pure, React-free logic: NCBI deep-link builders, feature/qualifier
 * parsing, related-resource and trust-badge derivation, and the GenBank
 * flat-file renderer. The `SequenceDetail` page and its presentational
 * parts consume these; keeping them here isolates the data-shaping
 * responsibility from the React rendering layer.
 */

import type { NuccoreFeature, NuccoreGenBank, NuccoreSummary } from "@/api/ncbi";
import { ncbiNucleotideByOrganismUrl, ncbiOrganismClause, ncbiTaxonomyUrl } from "./ncbiLinks";

export const NCBI_NUCCORE_BASE = "https://www.ncbi.nlm.nih.gov/nuccore";
// Standalone Sequence Viewer lives at the bare ``/projects/sviewer/`` path.
// The legacy ``sviewer.cgi`` endpoint now 404s, so never append it.
export const NCBI_SVIEWER_BASE = "https://www.ncbi.nlm.nih.gov/projects/sviewer/";

// Whole-sequence accession BLAST is rejected by the submit pipeline when the
// resolved FASTA exceeds 5 MiB (see ``ncbi_query_too_large`` in
// ``api/services/blast/accession_resolver.py``). We surface a confirm dialog
// before navigating to BLAST submit so researchers can opt out instead of
// hitting the error after the round trip.
export const MAX_WHOLE_SEQUENCE_NT = 5_000_000;

// Initial cap on rendered feature rows; "Show all" reveals the rest so a
// feature-dense genome record does not produce a multi-thousand-row table on
// first paint.
export const FEATURE_DISPLAY_LIMIT = 60;

export function externalNuccoreUrl(accession: string): string {
  return `${NCBI_NUCCORE_BASE}/${encodeURIComponent(accession)}`;
}

export function sviewerEmbedUrl(
  accession: string,
  highlight?: { start: number; stop: number } | null,
): string {
  const params = new URLSearchParams();
  params.set("id", accession);
  params.set("tracks", "[key:sequence_track,name:Sequence][key:gene_model_track]");
  if (highlight) {
    params.set("v", `${highlight.start}:${highlight.stop}`);
    params.set("mk", `${highlight.start}:${highlight.stop}|hit`);
  }
  return `${NCBI_SVIEWER_BASE}?${params.toString()}`;
}

export function formatInteger(value: number | null | undefined): string {
  if (value == null) return "—";
  return value.toLocaleString();
}

export function featureLabel(feature: NuccoreFeature): string {
  return feature.key || "feature";
}

/** First value of a named qualifier on a feature, or null. */
export function qualifier(feature: NuccoreFeature, name: string): string | null {
  for (const qual of feature.qualifiers) {
    if (qual.name === name && qual.value) return qual.value;
  }
  return null;
}

export function featureSummary(feature: NuccoreFeature): string {
  const parts: string[] = [];
  const gene = qualifier(feature, "gene");
  const product = qualifier(feature, "product");
  const note = qualifier(feature, "note");
  if (gene) parts.push(gene);
  if (product) parts.push(product);
  if (note && parts.length === 0) parts.push(note);
  return parts.join(" — ");
}

export function featureRange(
  feature: NuccoreFeature,
): { start: number; stop: number } | null {
  for (const interval of feature.intervals) {
    if (interval.start != null && interval.stop != null) {
      return { start: interval.start, stop: interval.stop };
    }
  }
  return null;
}

// NCBI renders sample provenance from the ``source`` feature qualifiers. We
// surface the same fields the public nuccore record shows so the dashboard
// detail page reads like the NCBI page. ``country`` is the legacy name for
// what NCBI now labels ``geo_loc_name``; accept either.
export const SOURCE_QUALIFIER_FIELDS: ReadonlyArray<{ label: string; names: string[] }> = [
  { label: "Mol type", names: ["mol_type"] },
  { label: "Isolate", names: ["isolate"] },
  { label: "Strain", names: ["strain"] },
  { label: "Host", names: ["host"] },
  { label: "Geo location", names: ["geo_loc_name", "country"] },
  { label: "Collection date", names: ["collection_date"] },
  { label: "Collected by", names: ["collected_by"] },
  { label: "Isolation source", names: ["isolation_source"] },
];

export function sourceFeature(genbank: NuccoreGenBank | undefined): NuccoreFeature | null {
  if (!genbank) return null;
  return genbank.features.find((feature) => feature.key === "source") ?? null;
}

export function firstQualifier(
  feature: NuccoreFeature | null,
  names: string[],
): string | null {
  if (!feature) return null;
  for (const name of names) {
    const value = qualifier(feature, name);
    if (value) return value;
  }
  return null;
}

const NCBI_BIOPROJECT_BASE = "https://www.ncbi.nlm.nih.gov/bioproject";
const NCBI_BIOSAMPLE_BASE = "https://www.ncbi.nlm.nih.gov/biosample";
export const NCBI_PUBMED_BASE = "https://pubmed.ncbi.nlm.nih.gov";
const NCBI_GENE_BASE = "https://www.ncbi.nlm.nih.gov/gene/";
const NCBI_GENE_SEARCH_BASE = "https://www.ncbi.nlm.nih.gov/gene/?term=";
const NCBI_CLINVAR_SEARCH_BASE = "https://www.ncbi.nlm.nih.gov/clinvar/?term=";
const NCBI_DBSNP_SEARCH_BASE = "https://www.ncbi.nlm.nih.gov/snp/?term=";
const NCBI_OMIM_SEARCH_BASE = "https://www.ncbi.nlm.nih.gov/omim/?term=";
export const DOI_BASE = "https://doi.org/";

export function xrefUrl(dbname: string, id: string): string | null {
  const key = dbname.toLowerCase();
  if (key === "bioproject") return `${NCBI_BIOPROJECT_BASE}/${encodeURIComponent(id)}`;
  if (key === "biosample") return `${NCBI_BIOSAMPLE_BASE}/${encodeURIComponent(id)}`;
  return null;
}

// A related NCBI resource the researcher commonly jumps to from a nuccore
// record. We surface these as deep-links rather than embedding (NCBI blocks
// cross-origin framing) so a molecular-diagnostics workflow can pivot to
// Gene / ClinVar / dbSNP / OMIM / Taxonomy in one click.
export interface RelatedResource {
  label: string;
  href: string;
  hint: string;
  /**
   * `exact` — the link resolves to a single curated record by stable id
   * (GeneID, taxid). `search` — the link is a symbol/organism text query that
   * may return several records, so the UI flags it as a search rather than a
   * guaranteed match. This keeps gene-symbol fallbacks from looking
   * authoritative on records where we never resolved a GeneID.
   */
  confidence: "exact" | "search";
}

/**
 * Collect the distinct gene symbols and the first GeneID db_xref from a
 * record's features. Transcript RefSeq records (NM_/NR_) typically carry a
 * single gene; genome records carry many, so the caller uses the count to
 * decide whether clinically-scoped links (ClinVar/dbSNP/OMIM) are meaningful.
 */
export function collectGeneInfo(
  genbank: NuccoreGenBank | undefined,
): { symbols: string[]; geneId: string | null } {
  if (!genbank) return { symbols: [], geneId: null };
  const symbols: string[] = [];
  let geneId: string | null = null;
  for (const feature of genbank.features) {
    for (const qual of feature.qualifiers) {
      if (qual.name === "gene" && qual.value) {
        const sym = qual.value.trim();
        if (sym && !symbols.includes(sym)) symbols.push(sym);
      }
      if (qual.name === "db_xref" && qual.value && geneId == null) {
        const match = /^GeneID:(\d+)$/i.exec(qual.value.trim());
        if (match) geneId = match[1];
      }
    }
  }
  return { symbols, geneId };
}

export function buildRelatedResources(opts: {
  symbols: string[];
  geneId: string | null;
  taxid: number | null;
  organism: string | null;
}): RelatedResource[] {
  const { symbols, geneId, taxid, organism } = opts;
  const out: RelatedResource[] = [];
  const singleGene = symbols.length === 1 ? symbols[0] : null;

  if (geneId) {
    // Resolved a stable GeneID from a db_xref \u2014 this is an exact, curated link.
    out.push({
      label: "Gene",
      href: `${NCBI_GENE_BASE}${geneId}`,
      hint: `GeneID ${geneId}`,
      confidence: "exact",
    });
  } else if (singleGene) {
    // No GeneID on the record — fall back to a symbol search, scoped by
    // organism when known. Flagged as a search so it never looks authoritative.
    const orgClause = ncbiOrganismClause({ taxid, organism });
    const term = orgClause ? `${singleGene}[gene] AND ${orgClause}` : `${singleGene}[gene]`;
    out.push({
      label: "Gene",
      href: `${NCBI_GENE_SEARCH_BASE}${encodeURIComponent(term)}`,
      hint: `${singleGene} (symbol)`,
      confidence: "search",
    });
  }

  // Clinical / variation resources only make sense for a single, unambiguous
  // gene \u2014 surfacing ClinVar for an arbitrarily-picked gene on a multi-gene
  // genome would mislead. Keep them gated on a single gene symbol. These are
  // inherently symbol searches, so they are always flagged as searches.
  if (singleGene) {
    out.push({
      label: "ClinVar",
      href: `${NCBI_CLINVAR_SEARCH_BASE}${encodeURIComponent(`${singleGene}[gene]`)}`,
      hint: `${singleGene} variants`,
      confidence: "search",
    });
    out.push({
      label: "dbSNP",
      href: `${NCBI_DBSNP_SEARCH_BASE}${encodeURIComponent(`${singleGene}[gene]`)}`,
      hint: `${singleGene} SNPs`,
      confidence: "search",
    });
    out.push({
      label: "OMIM",
      href: `${NCBI_OMIM_SEARCH_BASE}${encodeURIComponent(singleGene)}`,
      hint: `${singleGene} phenotypes`,
      confidence: "search",
    });
  }

  if (taxid != null) {
    out.push({
      label: "Taxonomy",
      href: ncbiTaxonomyUrl(taxid),
      hint: organism ?? `taxid ${taxid}`,
      confidence: "exact",
    });
  }
  // Prefer a taxid-scoped nucleotide search (`txid<N>[Organism:exp]`) — exact
  // and immune to the unquoted-multi-word-organism `[orgn]` binding bug. Only
  // fall back to a quoted organism phrase when no taxid resolved.
  const nucleotideHref = ncbiNucleotideByOrganismUrl({ taxid, organism });
  if (nucleotideHref) {
    out.push({
      label: "Nucleotide",
      href: nucleotideHref,
      hint: organism ? `${organism} records` : `taxid ${taxid} records`,
      confidence: taxid != null ? "exact" : "search",
    });
  }
  return out;
}

// A trust signal rendered as a pill in the record header. Molecular-diagnostics
// users will not commit to a sequence without knowing it is the current, live
// record and (for transcripts) the canonical MANE annotation. We derive these
// from data we already fetch (esummary status / replaced_by, GenBank keywords)
// rather than building a coordinate mapper.
export interface TrustBadge {
  tone: "ok" | "warn";
  label: string;
  title: string;
  /** Internal navigation target (the replacing accession), when applicable. */
  to?: string;
}

const _SUPERSEDED_STATUSES = ["suppressed", "withdrawn", "dead", "unverified"];

export function deriveTrustBadges(
  summary: NuccoreSummary | undefined,
  genbank: NuccoreGenBank | undefined,
): TrustBadge[] {
  const out: TrustBadge[] = [];
  const status = summary?.status ?? null;
  const replacedBy = summary?.replaced_by ?? null;

  if (replacedBy || status === "replaced") {
    out.push({
      tone: "warn",
      label: replacedBy ? `Replaced by ${replacedBy}` : "Replaced record",
      title:
        "NCBI has superseded this accession with a newer record. Verify against the" +
        " current version before using it diagnostically.",
      to: replacedBy ? `/sequence/${replacedBy}` : undefined,
    });
  } else if (status && _SUPERSEDED_STATUSES.includes(status)) {
    out.push({
      tone: "warn",
      label: `Record ${status}`,
      title:
        `NCBI marks this record as "${status}". Do not rely on it without checking` +
        " the current status on the NCBI record.",
    });
  } else if (status === "live") {
    out.push({
      tone: "ok",
      label: "Live record",
      title: "NCBI reports this accession version as the current, live record.",
    });
  }

  // MANE = Matched Annotation from NCBI and EMBL-EBI: the agreed canonical
  // transcript + coordinate reference for a gene. Surfacing the keyword answers
  // "is this the authoritative transcript?" without a full coordinate mapper.
  const mane = (genbank?.keywords ?? []).find((k) => /^MANE\b/i.test(k.trim()));
  if (mane) {
    out.push({
      tone: "ok",
      label: mane,
      title:
        "MANE (Matched Annotation from NCBI and EMBL-EBI) marks this as the canonical" +
        " transcript and coordinate reference for the gene.",
    });
  }
  return out;
}

// A feature ``db_xref`` qualifier value is ``dbname:id`` (e.g. ``taxon:9606``,
// ``GeneID:7157``). NCBI links the common databases; we mirror the most useful
// ones and leave the rest as plain text.
export function dbXrefUrl(value: string): { label: string; href: string | null } {
  const idx = value.indexOf(":");
  if (idx < 0) return { label: value, href: null };
  const db = value.slice(0, idx).trim();
  const id = value.slice(idx + 1).trim();
  const key = db.toLowerCase();
  if (key === "taxon") return { label: value, href: ncbiTaxonomyUrl(id) };
  if (key === "geneid") return { label: value, href: `${NCBI_GENE_BASE}${encodeURIComponent(id)}` };
  return { label: value, href: null };
}

// GenBank flat-file rendering. NCBI's nuccore page leads with the classic
// fixed-column header block (LOCUS / DEFINITION / ACCESSION / VERSION / DBLINK /
// KEYWORDS / SOURCE / ORGANISM). The dashboard already fetches every field via
// ``getNuccoreGenBank``, so we reproduce that block verbatim for researchers who
// read records in the canonical GenBank layout. Column geometry mirrors the
// real format: a 12-character tag field, 79-character lines, and continuation
// lines indented to column 13.
const GENBANK_TAG_WIDTH = 12;
const GENBANK_LINE_WIDTH = 79;

function genbankTag(tag: string): string {
  return (tag + " ".repeat(GENBANK_TAG_WIDTH)).slice(0, GENBANK_TAG_WIDTH);
}

/** Word-wrap ``value`` under a leading prefix, indenting continuation lines. */
function genbankWrap(value: string, firstPrefix: string, contPrefix: string): string[] {
  const avail = GENBANK_LINE_WIDTH - GENBANK_TAG_WIDTH;
  const words = value.split(/\s+/).filter(Boolean);
  if (words.length === 0) return [firstPrefix.trimEnd()];
  const out: string[] = [];
  let line = "";
  let prefix = firstPrefix;
  for (const word of words) {
    const candidate = line ? `${line} ${word}` : word;
    if (candidate.length > avail && line) {
      out.push(prefix + line);
      prefix = contPrefix;
      line = word;
    } else {
      line = candidate;
    }
  }
  out.push(prefix + line);
  return out;
}

export function genbankFlatLines(genbank: NuccoreGenBank): string[] {
  const indent = " ".repeat(GENBANK_TAG_WIDTH);
  const lines: string[] = [];

  const locusFields = [
    genbank.locus || genbank.accession,
    genbank.length != null ? `${genbank.length} bp` : "",
    genbank.moltype || "",
    genbank.topology || "",
    genbank.division || "",
    genbank.update_date || "",
  ].filter((part) => part.length > 0);
  lines.push(genbankTag("LOCUS") + locusFields.join("   "));

  lines.push(
    ...genbankWrap(genbank.definition || ".", genbankTag("DEFINITION"), indent),
  );
  const accessionLine = genbank.secondary_accessions.length
    ? `${genbank.accession} ${genbank.secondary_accessions.join(" ")}`
    : genbank.accession;
  lines.push(...genbankWrap(accessionLine, genbankTag("ACCESSION"), indent));
  lines.push(
    genbankTag("VERSION") + (genbank.accession_version || genbank.accession),
  );
  if (genbank.gi) {
    lines.push(genbankTag("VERSION") + `GI:${genbank.gi}`);
  }
  genbank.xrefs.forEach((xref, idx) => {
    const prefix = idx === 0 ? genbankTag("DBLINK") : indent;
    lines.push(`${prefix}${xref.dbname}: ${xref.id}`);
  });
  const keywords = genbank.keywords.length ? `${genbank.keywords.join("; ")}.` : ".";
  lines.push(genbankTag("KEYWORDS") + keywords);

  if (genbank.source || genbank.organism) {
    lines.push(
      ...genbankWrap(
        genbank.source || genbank.organism || ".",
        genbankTag("SOURCE"),
        indent,
      ),
    );
  }
  if (genbank.organism) {
    lines.push(genbankTag("  ORGANISM") + genbank.organism);
    const lineage = genbank.taxonomy_lineage.trim();
    if (lineage) {
      const terminated = lineage.endsWith(".") ? lineage : `${lineage}.`;
      lines.push(...genbankWrap(terminated, indent, indent));
    }
  }
  if (genbank.comment) {
    lines.push(...genbankWrap(genbank.comment, genbankTag("COMMENT"), indent));
  }
  return lines;
}
