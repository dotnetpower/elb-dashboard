/**
 * Shared formatters and color helpers for the BLAST results tabs.
 *
 * Extracted from the old `BlastAnalytics.tsx` so the same code drives the
 * Descriptions / Graphic Summary / Alignments / Taxonomy tabs rendered
 * inside the unified `BlastResults` page.
 */

export type BlastHitNumericInput = unknown;

export function numberValue(value: BlastHitNumericInput): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value !== "string") return null;
  const parsed = Number(value.trim());
  return Number.isFinite(parsed) ? parsed : null;
}

export function formatDecimal(value: BlastHitNumericInput, digits: number): string {
  const numeric = numberValue(value);
  return numeric === null ? "—" : numeric.toFixed(digits);
}

export function formatInteger(value: BlastHitNumericInput): string {
  const numeric = numberValue(value);
  return numeric === null ? "—" : Math.round(numeric).toLocaleString();
}

export function formatPercent(value: BlastHitNumericInput): string {
  const numeric = numberValue(value);
  return numeric === null ? "—" : `${numeric.toFixed(1)}%`;
}

export function formatRange(
  start: BlastHitNumericInput,
  end: BlastHitNumericInput,
): string {
  const startNumber = numberValue(start);
  const endNumber = numberValue(end);
  if (startNumber === null || endNumber === null) return "—";
  return `${Math.round(startNumber)}–${Math.round(endNumber)}`;
}

export function formatEvalue(value: BlastHitNumericInput): string {
  const ev = numberValue(value);
  if (ev === null) return "—";
  if (ev === 0) return "0";
  if (ev < 1e-100) return ev.toExponential(0);
  if (ev < 0.01) return ev.toExponential(1);
  if (ev < 1) return ev.toFixed(3);
  return ev.toFixed(1);
}

export function clampPercent(value: number): number {
  return Math.max(0, Math.min(100, value));
}

export function coverageOffset(start: number, end: number, total: number): number {
  if (total <= 0) return 0;
  return clampPercent(((Math.min(start, end) - 1) / total) * 100);
}

export function coverageWidth(start: number, end: number, total: number): number {
  if (total <= 0) return 0;
  return clampPercent(((Math.abs(end - start) + 1) / total) * 100);
}

/** Identity-percent palette (text/cell color in the hits table). */
export function identityColor(value: BlastHitNumericInput): string {
  const numeric = numberValue(value);
  if (numeric === null) return "var(--text-muted)";
  if (numeric >= 90) return "var(--success)";
  if (numeric >= 70) return "var(--warning)";
  return "var(--danger)";
}

/**
 * NCBI Web BLAST standard "Graphic Summary" score-binned palette.
 * Researchers recognise these colors instantly — applying them to the
 * Graphic Summary tab so hits are color-coded the way they are at NCBI.
 *
 *   < 40  : black
 *   40-50 : blue
 *   50-80 : green
 *   80-200: magenta
 *   ≥ 200 : red
 */
export type NcbiScoreBin = "<40" | "40-50" | "50-80" | "80-200" | ">=200";

export function ncbiScoreBin(bitscore: BlastHitNumericInput): NcbiScoreBin {
  const value = numberValue(bitscore) ?? 0;
  if (value < 40) return "<40";
  if (value < 50) return "40-50";
  if (value < 80) return "50-80";
  if (value < 200) return "80-200";
  return ">=200";
}

export const NCBI_SCORE_COLOR: Record<NcbiScoreBin, string> = {
  "<40": "#1c1f24",
  "40-50": "#4a78ff",
  "50-80": "#3fbf6a",
  "80-200": "#c850b0",
  ">=200": "#e0524a",
};

export const NCBI_SCORE_LABEL: Record<NcbiScoreBin, string> = {
  "<40": "< 40",
  "40-50": "40 – 50",
  "50-80": "50 – 80",
  "80-200": "80 – 200",
  ">=200": "≥ 200",
};

export function ncbiScoreColor(bitscore: BlastHitNumericInput): string {
  return NCBI_SCORE_COLOR[ncbiScoreBin(bitscore)];
}

/** "Plus" / "Minus" derived from subject start/end. */
export function strandLabel(
  sstart: BlastHitNumericInput,
  send: BlastHitNumericInput,
): "Plus/Plus" | "Plus/Minus" | "—" {
  const a = numberValue(sstart);
  const b = numberValue(send);
  if (a === null || b === null) return "—";
  return b >= a ? "Plus/Plus" : "Plus/Minus";
}

/** NCBI accession → nuccore deep link. Falls back to all-databases search. */
export function ncbiNuccoreUrl(accession: string): string {
  const trimmed = accession.split("|").pop()?.split(".")[0] ?? accession;
  return `https://www.ncbi.nlm.nih.gov/nuccore/${encodeURIComponent(trimmed)}`;
}

export function ncbiSearchUrl(accession: string): string {
  return `https://www.ncbi.nlm.nih.gov/search/all/?term=${encodeURIComponent(accession)}`;
}

/** NCBI Taxonomy browser deep link by taxid. */
export function ncbiTaxonomyUrl(taxid: string | number): string {
  return `https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id=${encodeURIComponent(
    String(taxid),
  )}`;
}

export function taxidLabel(value: string | undefined): string {
  if (!value) return "";
  return value
    .split(";")
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => `taxid:${part}`)
    .join(", ");
}

/**
 * Extract the first numeric taxid from a BLAST `staxids` field.
 *
 * BLAST may emit one or more taxids separated by `;` (and occasionally `,`)
 * when the alignment hits a sequence with multiple Taxonomy mappings. We
 * pick the leading entry so the Scientific Name modal can resolve a single
 * NCBI record directly instead of doing a name search. Returns `null` when
 * the field is empty or malformed.
 */
export function parseLeadingTaxid(value: string | null | undefined): number | null {
  if (!value) return null;
  const first = value.split(/[;,]/)[0]?.trim();
  if (!first) return null;
  const parsed = Number.parseInt(first, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

// Boundary tokens after which the remainder of a BLAST subject title is no
// longer the scientific name (NCBI titles look like "Monkeypox virus isolate
// 24MPX2634V genome assembly, complete genome"). Mirrors
// `api/services/blast/result_analytics.py::extract_organism_from_stitle` so
// the frontend can show NCBI's "Scientific Name" column for each row even
// before the server-side Taxonomy enrichment runs.
const STITLE_STOP_RE = new RegExp(
  "\\b(?:" +
    [
      "isolate",
      "strain",
      "clone",
      "chromosome",
      "complete",
      "partial",
      "genome",
      "sequence",
      "scaffold",
      "contig",
      "plasmid",
      "mitochond",
      "chloroplast",
      "segment",
      "cds",
      "mRNA",
      "rRNA",
      "tRNA",
      "ncRNA",
      "gene",
      "BAC",
    ].join("|") +
    ")\\b",
  "i",
);

const STITLE_LEADING_QUALIFIERS = [
  "PREDICTED:",
  "TPA:",
  "TPA_inf:",
  "UNVERIFIED:",
  "MAG:",
  "PARTIAL:",
  "LOW QUALITY PROTEIN:",
  "RecName:",
];

export function organismFromStitle(stitle: string | undefined): string {
  if (!stitle) return "";
  let text = String(stitle).trim();
  if (!text) return "";
  if (
    text.includes("|") &&
    /^(gi|ref|gb|emb|dbj|sp|tr)\|/.test(text)
  ) {
    text = text.includes(" ") ? text.split(" ").slice(1).join(" ") : "";
  }
  text = text.replace(/^>+/, "").trim();
  let changed = true;
  while (changed && text) {
    changed = false;
    for (const qualifier of STITLE_LEADING_QUALIFIERS) {
      if (text.slice(0, qualifier.length).toUpperCase() === qualifier.toUpperCase()) {
        text = text.slice(qualifier.length).trim();
        changed = true;
        break;
      }
    }
  }
  if (!text) return "";
  const stopMatch = STITLE_STOP_RE.exec(text);
  let cutoff = stopMatch ? stopMatch.index : text.length;
  const comma = text.indexOf(",");
  if (comma >= 0 && comma < cutoff) cutoff = comma;
  const paren = text.indexOf("(");
  if (paren >= 0 && paren < cutoff) cutoff = paren;
  const candidate = text.slice(0, cutoff).replace(/^[\s,.:;-]+|[\s,.:;-]+$/g, "");
  const tokens = candidate.split(/\s+/).filter(Boolean);
  if (tokens.length < 1 || tokens.length > 6) return "";
  if (tokens[0].length < 2 || /^\d+$/.test(tokens[0])) return "";
  return tokens.join(" ");
}

export function shortBlobName(value: string | undefined): string {
  if (!value) return "—";
  const parts = value.split("/").filter(Boolean);
  return parts.at(-1) ?? value;
}

export function parsePercentInput(value: string): number {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 0;
  return Math.max(0, Math.min(100, numeric));
}

export function parseNonNegativeInput(value: string, fallback: number): number {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return fallback;
  return Math.max(0, numeric);
}

export function isPartialResult(
  data:
    | {
        degraded?: boolean;
        degraded_reason?: string;
        truncated?: boolean;
        hit_limit_reached?: boolean;
        read_failures?: number;
      }
    | undefined,
): data is NonNullable<typeof data> {
  return Boolean(
    data?.degraded ||
    data?.truncated ||
    data?.hit_limit_reached ||
    (data?.read_failures ?? 0) > 0,
  );
}

export function isResultFilesUnavailable(
  data: { degraded_reason?: string } | undefined,
): boolean {
  return (
    data?.degraded_reason === "no_result_files" ||
    data?.degraded_reason === "storage_unreachable"
  );
}
