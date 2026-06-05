import { useMemo, useState, type CSSProperties } from "react";
import { useTransientState } from "../../hooks/useTransientState";
import { Link, useParams, useNavigate, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  ExternalLink,
  ArrowLeft,
  Play,
  AlertTriangle,
  Maximize2,
  Copy,
  Check,
  ChevronRight,
  ChevronDown,
} from "lucide-react";

import {
  getNuccoreSummary,
  getNuccoreGenBank,
  getNuccoreFasta,
  type NuccoreFeature,
  type NuccoreGenBank,
  type NuccoreSummary,
} from "@/api/ncbi";
import { SViewerEmbed } from "./SViewerEmbed";
import { SequenceBlocks } from "./SequenceBlocks";
import { JobBackReferenceCard } from "./JobBackReferenceCard";
import { ncbiTaxonomyUrl, ncbiNucleotideByOrganismUrl, ncbiOrganismClause } from "./ncbiLinks";

const NCBI_NUCCORE_BASE = "https://www.ncbi.nlm.nih.gov/nuccore";
// Standalone Sequence Viewer lives at the bare ``/projects/sviewer/`` path.
// The legacy ``sviewer.cgi`` endpoint now 404s, so never append it.
const NCBI_SVIEWER_BASE = "https://www.ncbi.nlm.nih.gov/projects/sviewer/";

// Whole-sequence accession BLAST is rejected by the submit pipeline when the
// resolved FASTA exceeds 5 MiB (see ``ncbi_query_too_large`` in
// ``api/services/blast/accession_resolver.py``). We surface a confirm dialog
// before navigating to BLAST submit so researchers can opt out instead of
// hitting the error after the round trip.
const MAX_WHOLE_SEQUENCE_NT = 5_000_000;

function externalNuccoreUrl(accession: string): string {
  return `${NCBI_NUCCORE_BASE}/${encodeURIComponent(accession)}`;
}

function sviewerEmbedUrl(
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

function formatInteger(value: number | null | undefined): string {
  if (value == null) return "—";
  return value.toLocaleString();
}

function featureLabel(feature: NuccoreFeature): string {
  return feature.key || "feature";
}

/** First value of a named qualifier on a feature, or null. */
function qualifier(feature: NuccoreFeature, name: string): string | null {
  for (const qual of feature.qualifiers) {
    if (qual.name === name && qual.value) return qual.value;
  }
  return null;
}

function featureSummary(feature: NuccoreFeature): string {
  const parts: string[] = [];
  const gene = qualifier(feature, "gene");
  const product = qualifier(feature, "product");
  const note = qualifier(feature, "note");
  if (gene) parts.push(gene);
  if (product) parts.push(product);
  if (note && parts.length === 0) parts.push(note);
  return parts.join(" — ");
}

function featureRange(feature: NuccoreFeature): { start: number; stop: number } | null {
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
const SOURCE_QUALIFIER_FIELDS: ReadonlyArray<{ label: string; names: string[] }> = [
  { label: "Mol type", names: ["mol_type"] },
  { label: "Isolate", names: ["isolate"] },
  { label: "Strain", names: ["strain"] },
  { label: "Host", names: ["host"] },
  { label: "Geo location", names: ["geo_loc_name", "country"] },
  { label: "Collection date", names: ["collection_date"] },
  { label: "Collected by", names: ["collected_by"] },
  { label: "Isolation source", names: ["isolation_source"] },
];

function sourceFeature(genbank: NuccoreGenBank | undefined): NuccoreFeature | null {
  if (!genbank) return null;
  return genbank.features.find((feature) => feature.key === "source") ?? null;
}

function firstQualifier(
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
const NCBI_PUBMED_BASE = "https://pubmed.ncbi.nlm.nih.gov";
const NCBI_GENE_BASE = "https://www.ncbi.nlm.nih.gov/gene/";
const NCBI_GENE_SEARCH_BASE = "https://www.ncbi.nlm.nih.gov/gene/?term=";
const NCBI_CLINVAR_SEARCH_BASE = "https://www.ncbi.nlm.nih.gov/clinvar/?term=";
const NCBI_DBSNP_SEARCH_BASE = "https://www.ncbi.nlm.nih.gov/snp/?term=";
const NCBI_OMIM_SEARCH_BASE = "https://www.ncbi.nlm.nih.gov/omim/?term=";
const DOI_BASE = "https://doi.org/";

function xrefUrl(dbname: string, id: string): string | null {
  const key = dbname.toLowerCase();
  if (key === "bioproject") return `${NCBI_BIOPROJECT_BASE}/${encodeURIComponent(id)}`;
  if (key === "biosample") return `${NCBI_BIOSAMPLE_BASE}/${encodeURIComponent(id)}`;
  return null;
}

// A related NCBI resource the researcher commonly jumps to from a nuccore
// record. We surface these as deep-links rather than embedding (NCBI blocks
// cross-origin framing) so a molecular-diagnostics workflow can pivot to
// Gene / ClinVar / dbSNP / OMIM / Taxonomy in one click.
interface RelatedResource {
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
function collectGeneInfo(
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

function buildRelatedResources(opts: {
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
interface TrustBadge {
  tone: "ok" | "warn";
  label: string;
  title: string;
  /** Internal navigation target (the replacing accession), when applicable. */
  to?: string;
}

const _SUPERSEDED_STATUSES = ["suppressed", "withdrawn", "dead", "unverified"];

function deriveTrustBadges(
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
function dbXrefUrl(value: string): { label: string; href: string | null } {
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

function genbankFlatLines(genbank: NuccoreGenBank): string[] {
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

export function SequenceDetail() {
  const params = useParams<{ accession: string }>();
  const accession = (params.accession || "").trim();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const highlightStart = Number.parseInt(searchParams.get("hl_start") || "", 10);
  const highlightStop = Number.parseInt(searchParams.get("hl_stop") || "", 10);
  const hasHighlight =
    Number.isFinite(highlightStart) &&
    Number.isFinite(highlightStop) &&
    highlightStart > 0 &&
    highlightStop >= highlightStart;

  const summaryQuery = useQuery({
    queryKey: ["ncbi", "summary", accession],
    queryFn: () => getNuccoreSummary(accession),
    enabled: accession.length > 0,
    staleTime: 5 * 60_000,
  });

  // The summary call is cheap (single ESummary round trip). GenBank parsing
  // and FASTA download are heavier and only meaningful once the summary
  // confirmed the accession exists, so we chain ``enabled`` to avoid firing
  // three parallel NCBI calls for a typo (e.g. `XX_999999.9`) — under the
  // shared 3 req/s rate limit that would burn the entire window.
  const genbankQuery = useQuery({
    queryKey: ["ncbi", "genbank", accession],
    queryFn: () => getNuccoreGenBank(accession),
    enabled: accession.length > 0 && !!summaryQuery.data,
    staleTime: 5 * 60_000,
  });

  const fastaQuery = useQuery({
    queryKey: ["ncbi", "fasta", accession],
    queryFn: () => getNuccoreFasta(accession),
    enabled: accession.length > 0 && !!genbankQuery.data,
    staleTime: 5 * 60_000,
  });

  const summary = summaryQuery.data;
  const genbank = genbankQuery.data;
  const fasta = fastaQuery.data;
  const highlightRange = hasHighlight
    ? { start: highlightStart, stop: highlightStop }
    : null;

  // Show the full resolved FASTA; the record is already bounded by the
  // backend fetch byte caps (MAX_FASTA_BYTES), so no display truncation here.
  const previewFasta = fasta;

  const source = sourceFeature(genbank);
  const sourceRows = useMemo(
    () =>
      SOURCE_QUALIFIER_FIELDS.map((field) => ({
        label: field.label,
        value: firstQualifier(source, field.names),
      })).filter((row) => row.value != null),
    [source],
  );
  const lineage = useMemo(() => {
    const raw = genbank?.taxonomy_lineage || "";
    return raw
      .split(";")
      .map((part) => part.trim())
      .filter((part) => part.length > 0);
  }, [genbank?.taxonomy_lineage]);

  const flatRecord = useMemo(
    () => (genbank ? genbankFlatLines(genbank).join("\n") : null),
    [genbank],
  );

  const relatedResources = useMemo(() => {
    const { symbols, geneId } = collectGeneInfo(genbank);
    return buildRelatedResources({
      symbols,
      geneId,
      taxid: summary?.taxid ?? null,
      organism: summary?.organism ?? genbank?.organism ?? null,
    });
  }, [genbank, summary?.taxid, summary?.organism]);

  const trustBadges = useMemo(
    () => deriveTrustBadges(summary, genbank),
    [summary, genbank],
  );

  if (!accession) {
    return (
      <div className="glass-card glass-card--strong" style={{ padding: 24 }}>
        <h2 style={{ marginTop: 0 }}>Missing accession</h2>
        <p className="muted">No NCBI accession was provided in the URL.</p>
        <Link className="glass-button" to="/">
          <ArrowLeft size={14} /> Back to dashboard
        </Link>
      </div>
    );
  }

  const launchBlast = () => {
    // Length guard: warn before navigating when the whole-sequence FASTA
    // would exceed the submit pipeline's 5 MiB cap. Sub-range BLAST always
    // proceeds because the caller already picked a window.
    if (!hasHighlight && summary === undefined) {
      // Summary still loading: the button is rendered disabled in this
      // state, but defend the click path against any future re-entry.
      return;
    }
    if (!hasHighlight && summary?.length && summary.length > MAX_WHOLE_SEQUENCE_NT) {
      const confirmed = window.confirm(
        `Whole-sequence BLAST for ${accession} (${summary.length.toLocaleString()} nt)` +
          ` exceeds the 5,000,000 nt limit and will fail at submit with` +
          ` "ncbi_query_too_large". Pick a sub-range from the hits table or` +
          ` BLAST a slice manually instead. Continue anyway?`,
      );
      if (!confirmed) return;
    }
    const params = new URLSearchParams();
    params.set("accession", accession);
    if (hasHighlight) {
      params.set("from", String(highlightStart));
      params.set("to", String(highlightStop));
    }
    navigate(`/blast/submit?${params.toString()}`);
  };

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div
        className="glass-card glass-card--strong"
        style={{ padding: 20, display: "grid", gap: 12 }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "flex-start",
            justifyContent: "space-between",
            gap: 16,
            flexWrap: "wrap",
          }}
        >
          <div style={{ minWidth: 0 }}>
            <div
              style={{
                display: "flex",
                gap: 6,
                alignItems: "center",
                fontSize: 12,
                color: "var(--text-muted)",
              }}
            >
              <Link to="/blast/jobs" style={{ color: "inherit" }}>
                ← BLAST jobs
              </Link>
              <span>/</span>
              <span>Sequence</span>
            </div>
            <h1
              style={{
                margin: "4px 0 0",
                fontSize: 22,
                fontFamily: "var(--font-mono, monospace)",
                wordBreak: "break-all",
              }}
            >
              {summary?.accession_version || accession}
            </h1>
            <div
              style={{
                marginTop: 6,
                fontSize: 13,
                color: "var(--text)",
                lineHeight: 1.4,
              }}
            >
              {summary?.title || genbank?.definition || (
                <span className="muted">Loading metadata…</span>
              )}
            </div>
            {trustBadges.length > 0 && (
              <div
                style={{
                  marginTop: 8,
                  display: "flex",
                  flexWrap: "wrap",
                  gap: 6,
                }}
              >
                {trustBadges.map((badge) => (
                  <TrustBadgePill key={badge.label} badge={badge} />
                ))}
              </div>
            )}
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button
              type="button"
              className="glass-button glass-button--primary"
              onClick={launchBlast}
              disabled={!hasHighlight && summary === undefined}
              title={
                !hasHighlight && summary === undefined
                  ? "Waiting for NCBI summary before BLAST handoff"
                  : undefined
              }
            >
              <Play size={14} strokeWidth={1.5} /> Use in BLAST
            </button>
            <a
              className="glass-button"
              href={externalNuccoreUrl(accession)}
              target="_blank"
              rel="noopener noreferrer"
            >
              <ExternalLink size={14} strokeWidth={1.5} /> Open in NCBI
            </a>
            <CopyButton
              value={summary?.accession_version || accession}
              label="Copy accession"
            />
          </div>
        </div>

        {(summaryQuery.isError || genbankQuery.isError || fastaQuery.isError) && (
          <div
            className="glass-card"
            role="alert"
            style={{
              padding: "10px 12px",
              display: "flex",
              alignItems: "center",
              gap: 8,
              color: "var(--warning)",
              fontSize: 12,
            }}
          >
            <AlertTriangle size={13} strokeWidth={1.5} />
            <span>
              One or more NCBI lookups failed. The dashboard does not cache
              upstream outages — retry in a moment.
            </span>
          </div>
        )}

        <dl
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
            gap: 12,
            margin: 0,
            fontSize: 13,
          }}
        >
          <MetaCell label="Organism" value={summary?.organism || genbank?.organism} />
          <MetaCell label="Taxid" value={summary?.taxid != null ? String(summary.taxid) : null} />
          <MetaCell label="Length (bp/aa)" value={formatInteger(summary?.length ?? genbank?.length ?? null)} />
          <MetaCell label="Molecule" value={summary?.moltype || genbank?.moltype} />
          <MetaCell label="Topology" value={summary?.topology || genbank?.topology} />
          <MetaCell label="Strandedness" value={genbank?.strandedness} />
          <MetaCell label="Biomol" value={summary?.biomol} />
          <MetaCell label="Completeness" value={summary?.completeness} />
          <MetaCell label="Division" value={genbank?.division} />
          <MetaCell label="Source DB" value={summary?.source_db} />
          <MetaCell label="Created" value={summary?.create_date || genbank?.create_date} />
          <MetaCell label="Updated" value={summary?.update_date || genbank?.update_date} />
        </dl>
      </div>

      <JobBackReferenceCard accession={accession} />

      {flatRecord && (
        <div
          className="glass-card glass-card--strong"
          style={{ padding: 16, display: "grid", gap: 10 }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 8,
              flexWrap: "wrap",
            }}
          >
            <h2 style={{ margin: 0, fontSize: 14 }}>GenBank record</h2>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
              Header block in the NCBI flat-file layout
            </span>
          </div>
          {genbank?.truncated_fields?.some((f) =>
            f === "definition" || f === "taxonomy_lineage",
          ) && (
            <TruncationNote href={externalNuccoreUrl(accession)} />
          )}
          <pre
            style={{
              margin: 0,
              padding: "12px 14px",
              overflowX: "auto",
              fontFamily: "var(--font-mono, monospace)",
              fontSize: 12,
              lineHeight: 1.6,
              color: "var(--text)",
              background: "var(--surface-2, rgba(255, 255, 255, 0.03))",
              borderRadius: 8,
              whiteSpace: "pre",
            }}
          >
            {flatRecord}
          </pre>
        </div>
      )}

      {(sourceRows.length > 0 || (genbank?.xrefs.length ?? 0) > 0) && (
        <div className="glass-card glass-card--strong" style={{ padding: 16, display: "grid", gap: 12 }}>
          <h2 style={{ margin: 0, fontSize: 14 }}>Sample &amp; source</h2>
          {sourceRows.length > 0 && (
            <dl
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
                gap: 12,
                margin: 0,
                fontSize: 13,
              }}
            >
              {sourceRows.map((row) => (
                <MetaCell key={row.label} label={row.label} value={row.value} />
              ))}
            </dl>
          )}
          {genbank && genbank.xrefs.length > 0 && (
            <div style={{ display: "grid", gap: 6 }}>
              <span style={{ fontSize: 11, color: "var(--text-muted)" }}>DBLINK</span>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                {genbank.xrefs.map((xref) => {
                  const href = xrefUrl(xref.dbname, xref.id);
                  const label = `${xref.dbname}: ${xref.id}`;
                  return href ? (
                    <a
                      key={`${xref.dbname}-${xref.id}`}
                      className="glass-button glass-button--ghost"
                      href={href}
                      target="_blank"
                      rel="noopener noreferrer"
                      style={{
                        fontSize: 12,
                        padding: "2px 8px",
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 4,
                      }}
                    >
                      <ExternalLink size={11} strokeWidth={1.5} />
                      {label}
                    </a>
                  ) : (
                    <span
                      key={`${xref.dbname}-${xref.id}`}
                      style={{
                        fontSize: 12,
                        padding: "2px 8px",
                        fontFamily: "var(--font-mono, monospace)",
                        color: "var(--text-muted)",
                      }}
                    >
                      {label}
                    </span>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}

      {lineage.length > 0 && (
        <div className="glass-card glass-card--strong" style={{ padding: 16, display: "grid", gap: 8 }}>
          <h2 style={{ margin: 0, fontSize: 14 }}>Taxonomy</h2>
          <div style={{ fontSize: 12, lineHeight: 1.7, color: "var(--text)" }}>
            {lineage.map((rank, idx) => (
              <span key={`${rank}-${idx}`}>
                {idx > 0 && <span style={{ color: "var(--text-muted)" }}> › </span>}
                {rank}
              </span>
            ))}
          </div>
        </div>
      )}

      {relatedResources.length > 0 && (
        <div className="glass-card glass-card--strong" style={{ padding: 16, display: "grid", gap: 10 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 8,
              flexWrap: "wrap",
            }}
          >
            <h2 style={{ margin: 0, fontSize: 14 }}>Related NCBI resources</h2>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
              Opens on the NCBI origin in a new tab
            </span>
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {relatedResources.map((resource) => (
              <a
                key={resource.label}
                className="glass-button glass-button--ghost"
                href={resource.href}
                target="_blank"
                rel="noopener noreferrer"
                title={
                  resource.confidence === "search"
                    ? `${resource.hint} — NCBI search (may return several records)`
                    : resource.hint
                }
                style={{
                  fontSize: 12,
                  padding: "4px 10px",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                }}
              >
                <ExternalLink size={12} strokeWidth={1.5} />
                <span style={{ fontWeight: 500 }}>{resource.label}</span>
                <span style={{ color: "var(--text-muted)" }}>· {resource.hint}</span>
                {resource.confidence === "search" && (
                  <span
                    style={{
                      fontSize: 10,
                      color: "var(--text-muted)",
                      border: "1px solid color-mix(in srgb, var(--text-muted) 35%, transparent)",
                      borderRadius: 4,
                      padding: "0 4px",
                      lineHeight: 1.5,
                    }}
                  >
                    search
                  </span>
                )}
              </a>
            ))}
          </div>
        </div>
      )}

      {genbank?.comment && (
        <div className="glass-card glass-card--strong" style={{ padding: 16, display: "grid", gap: 8 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 8,
              flexWrap: "wrap",
            }}
          >
            <h2 style={{ margin: 0, fontSize: 14 }}>Comment</h2>
            {genbank.truncated_fields?.includes("comment") && (
              <TruncationNote href={externalNuccoreUrl(accession)} />
            )}
          </div>
          <p
            style={{
              margin: 0,
              fontSize: 12,
              lineHeight: 1.6,
              color: "var(--text)",
              whiteSpace: "pre-wrap",
            }}
          >
            {genbank.comment}
          </p>
        </div>
      )}

      <div className="glass-card glass-card--strong" style={{ padding: 16, display: "grid", gap: 10 }}>
        <h2 style={{ margin: 0, fontSize: 14 }}>Features</h2>
        {genbankQuery.isLoading && <p className="muted" style={{ margin: 0 }}>Loading features…</p>}
        {genbank && genbank.features.length === 0 && (
          <p className="muted" style={{ margin: 0 }}>No features reported.</p>
        )}
        {genbank && genbank.features.length > 0 && (
          <div style={{ overflowX: "auto" }}>
            <table className="glass-table" style={{ width: "100%", fontSize: 12 }}>
              <thead>
                <tr>
                  <th style={{ width: 28 }} />
                  <th style={{ textAlign: "left" }}>Key</th>
                  <th style={{ textAlign: "left" }}>Location</th>
                  <th style={{ textAlign: "left" }}>Gene / Product</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {genbank.features.map((feature, idx) => (
                  <FeatureRow
                    key={`${feature.key}-${idx}`}
                    feature={feature}
                    nuccoreUrl={externalNuccoreUrl(accession)}
                    onBlastRange={(range) => {
                      const search = new URLSearchParams({
                        accession,
                        from: String(range.start),
                        to: String(range.stop),
                      });
                      navigate(`/blast/submit?${search.toString()}`);
                    }}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {genbank && genbank.references.length > 0 && (
        <div className="glass-card glass-card--strong" style={{ padding: 16, display: "grid", gap: 10 }}>
          <h2 style={{ margin: 0, fontSize: 14 }}>References</h2>
          <ol style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 10 }}>
            {genbank.references.map((ref, idx) => (
              <li key={`ref-${idx}`} style={{ fontSize: 12, lineHeight: 1.5 }}>
                {ref.reference && (
                  <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
                    REFERENCE {ref.reference}
                  </div>
                )}
                {ref.title && <div style={{ color: "var(--text)" }}>{ref.title}</div>}
                {ref.authors.length > 0 && (
                  <div className="muted">{ref.authors.join(", ")}</div>
                )}
                {ref.consortium && <div className="muted">Consortium: {ref.consortium}</div>}
                {ref.journal && <div className="muted">{ref.journal}</div>}
                {ref.remark && (
                  <div className="muted" style={{ fontStyle: "italic" }}>{ref.remark}</div>
                )}
                <div style={{ display: "flex", flexWrap: "wrap", gap: 12, marginTop: 2 }}>
                  {ref.pubmed && (
                    <a
                      href={`${NCBI_PUBMED_BASE}/${encodeURIComponent(ref.pubmed)}/`}
                      target="_blank"
                      rel="noopener noreferrer"
                      style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
                    >
                      <ExternalLink size={11} strokeWidth={1.5} />
                      PubMed {ref.pubmed}
                    </a>
                  )}
                  {ref.doi && (
                    <a
                      href={`${DOI_BASE}${encodeURIComponent(ref.doi)}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
                    >
                      <ExternalLink size={11} strokeWidth={1.5} />
                      doi:{ref.doi}
                    </a>
                  )}
                </div>
              </li>
            ))}
          </ol>
        </div>
      )}

      <div className="glass-card glass-card--strong" style={{ padding: 16, display: "grid", gap: 8 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
          <h2 style={{ margin: 0, fontSize: 14 }}>Sequence</h2>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            {hasHighlight && (
              <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
                Hit range {highlightStart.toLocaleString()}–{highlightStop.toLocaleString()} requested
              </span>
            )}
            {fasta && <CopyButton value={fasta} label="Copy FASTA" title="Copy the full FASTA to the clipboard" />}
          </div>
        </div>
        {fastaQuery.isLoading && <p className="muted" style={{ margin: 0 }}>Loading FASTA…</p>}
        {previewFasta && <SequenceBlocks fasta={previewFasta} highlight={highlightRange} />}
      </div>

      <div className="glass-card glass-card--strong" style={{ padding: 16, display: "grid", gap: 8 }}>
        <h2 style={{ margin: 0, fontSize: 14 }}>
          <Maximize2 size={13} strokeWidth={1.5} style={{ verticalAlign: "-2px", marginRight: 4 }} />
          Advanced view (NCBI Sequence Viewer)
        </h2>
        <p className="muted" style={{ margin: 0, fontSize: 12 }}>
          Pan / zoom the sequence and inspect tracks without leaving the
          dashboard. The interactive viewer is loaded from NCBI only when you
          click below; until then no NCBI script runs and no data leaves your
          browser. The viewer carries the accession
          {hasHighlight ? " and hit range" : ""} only.
        </p>
        {/* Key by accession so navigating to a different record resets the
            embed to its idle (not-yet-loaded) state instead of reusing the
            previous accession's viewer instance. */}
        <SViewerEmbed
          key={accession}
          accession={accession}
          highlight={highlightRange}
          fallbackHref={sviewerEmbedUrl(accession, highlightRange)}
        />
      </div>
    </div>
  );
}

function MetaCell({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <div style={{ display: "grid", gap: 2 }}>
      <dt style={{ fontSize: 11, color: "var(--text-muted)" }}>{label}</dt>
      <dd
        style={{
          margin: 0,
          fontFamily: label === "Updated" ? "inherit" : "var(--font-mono, monospace)",
        }}
      >
        {value || "—"}
      </dd>
    </div>
  );
}

// Record-trust pill. ``warn`` badges carry a muted warning tint; ``ok`` badges
// stay in the calm grey/teal family per the glass design rules. When a badge
// points at a replacing accession it renders as an internal Link so the user
// can jump straight to the current record.
function TrustBadgePill({ badge }: { badge: TrustBadge }) {
  const tint =
    badge.tone === "warn"
      ? { color: "var(--warning)", border: "var(--warning)" }
      : { color: "var(--text-muted)", border: "var(--border, var(--text-muted))" };
  const style: CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    gap: 5,
    fontSize: 11,
    lineHeight: 1.2,
    padding: "3px 9px",
    borderRadius: 999,
    border: `1px solid color-mix(in srgb, ${tint.border} 45%, transparent)`,
    background: `color-mix(in srgb, ${tint.color} 10%, transparent)`,
    color: tint.color,
    textDecoration: "none",
    whiteSpace: "nowrap",
  };
  const inner = (
    <>
      {badge.tone === "warn" ? (
        <AlertTriangle size={11} strokeWidth={1.5} />
      ) : (
        <Check size={11} strokeWidth={1.5} />
      )}
      <span style={{ fontWeight: 500 }}>{badge.label}</span>
    </>
  );
  if (badge.to) {
    return (
      <Link to={badge.to} title={badge.title} style={style}>
        {inner}
      </Link>
    );
  }
  return (
    <span title={badge.title} style={style}>
      {inner}
    </span>
  );
}

// Inline "this value was clipped" marker. Points the researcher at the full
// record on NCBI so a truncated value is never mistaken for the whole.
function TruncationNote({ href }: { href: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      title="This value was clipped for display. Open the full record on NCBI."
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        fontSize: 11,
        color: "var(--warning)",
        textDecoration: "none",
        whiteSpace: "nowrap",
      }}
    >
      <AlertTriangle size={11} strokeWidth={1.5} />
      truncated — view full on NCBI
    </a>
  );
}

// Copy-to-clipboard control. Mirrors NCBI's per-field copy affordance so a
// researcher can grab the accession or the FASTA without manual selection.
// Falls back silently if the Clipboard API is unavailable (older browsers /
// insecure context) — the button simply does nothing rather than throwing.
function CopyButton({
  value,
  label,
  title,
}: {
  value: string;
  label: string;
  title?: string;
}) {
  const [copied, flashCopied] = useTransientState(false);
  const onCopy = () => {
    if (!navigator.clipboard?.writeText) return;
    void navigator.clipboard.writeText(value).then(() => {
      flashCopied(true, 1500);
    });
  };
  return (
    <button
      type="button"
      className="glass-button glass-button--ghost"
      onClick={onCopy}
      title={title || `Copy ${label}`}
      style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11, padding: "2px 8px" }}
    >
      {copied ? <Check size={12} strokeWidth={1.5} /> : <Copy size={12} strokeWidth={1.5} />}
      {copied ? "Copied" : label}
    </button>
  );
}

// A single feature row plus an expandable panel that lists every qualifier.
// NCBI's nuccore page shows the full qualifier set (mol_type, isolate,
// db_xref, translation, …); the collapsed dashboard table only surfaces
// gene/product/note, so the toggle reveals parity on demand.
function FeatureRow({
  feature,
  nuccoreUrl,
  onBlastRange,
}: {
  feature: NuccoreFeature;
  nuccoreUrl: string;
  onBlastRange: (range: { start: number; stop: number }) => void;
}) {
  const [open, setOpen] = useState(false);
  const range = featureRange(feature);
  const hasQualifiers = feature.qualifiers.length > 0;
  return (
    <>
      <tr>
        <td style={{ textAlign: "center" }}>
          {hasQualifiers && (
            <button
              type="button"
              aria-label={open ? "Collapse qualifiers" : "Expand qualifiers"}
              aria-expanded={open}
              onClick={() => setOpen((prev) => !prev)}
              style={{
                background: "none",
                border: "none",
                cursor: "pointer",
                color: "var(--text-muted)",
                padding: 0,
                display: "inline-flex",
              }}
            >
              {open ? (
                <ChevronDown size={14} strokeWidth={1.5} />
              ) : (
                <ChevronRight size={14} strokeWidth={1.5} />
              )}
            </button>
          )}
        </td>
        <td style={{ fontFamily: "var(--font-mono, monospace)" }}>{featureLabel(feature)}</td>
        <td style={{ fontFamily: "var(--font-mono, monospace)" }}>{feature.location || "—"}</td>
        <td>{featureSummary(feature) || "—"}</td>
        <td style={{ textAlign: "right" }}>
          {range && (
            <button
              type="button"
              className="glass-button glass-button--ghost"
              style={{ fontSize: 11, padding: "2px 8px" }}
              onClick={() => onBlastRange(range)}
            >
              BLAST range
            </button>
          )}
        </td>
      </tr>
      {open && hasQualifiers && (
        <tr>
          <td />
          <td colSpan={4} style={{ paddingBottom: 10 }}>
            <dl
              style={{
                margin: 0,
                display: "grid",
                gridTemplateColumns: "minmax(120px, max-content) 1fr",
                gap: "2px 12px",
                fontSize: 11,
              }}
            >
              {feature.qualifiers.map((qual, qIdx) => (
                <FragmentQualifier
                  key={`${qual.name}-${qIdx}`}
                  name={qual.name}
                  value={qual.value}
                  truncated={qual.truncated}
                  nuccoreUrl={nuccoreUrl}
                />
              ))}
            </dl>
          </td>
        </tr>
      )}
    </>
  );
}

// One qualifier key/value pair. ``db_xref`` values are linked to the matching
// NCBI database (Taxonomy / Gene) when recognised. When the backend clipped the
// value (e.g. a long ``translation``), a marker links to the full NCBI record.
function FragmentQualifier({
  name,
  value,
  truncated,
  nuccoreUrl,
}: {
  name: string;
  value: string | null;
  truncated?: boolean;
  nuccoreUrl: string;
}) {
  const isDbXref = name === "db_xref" && value != null;
  const linked = isDbXref ? dbXrefUrl(value as string) : null;
  return (
    <>
      <dt style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono, monospace)" }}>
        /{name}
      </dt>
      <dd style={{ margin: 0, wordBreak: "break-word" }}>
        {linked?.href ? (
          <a href={linked.href} target="_blank" rel="noopener noreferrer">
            {linked.label}
          </a>
        ) : (
          value || "—"
        )}
        {truncated && (
          <>
            {" "}
            <TruncationNote href={nuccoreUrl} />
          </>
        )}
      </dd>
    </>
  );
}

export default SequenceDetail;
