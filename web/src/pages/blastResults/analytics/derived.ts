/**
 * Pure derivation helpers for the BLAST result "beyond NCBI" panels.
 *
 * These functions turn the raw `BlastHit[]` page (and the job's provenance
 * bundle) into the shapes the differentiated result views consume:
 *   - Coverage × Identity triage scatter (`buildTriagePoints`)
 *   - Taxonomic dereplication rollup (`derepByRank`)
 *   - Subject coordinate / multi-HSP map (`buildSubjectTracks`)
 *   - Plain-language hit evidence (`evalueConfidence`, `searchSpacePin`)
 *   - Result Passport methods text + parity verdict (`buildMethodsText`,
 *     `parityVerdict`)
 *
 * Everything here is intentionally pure and DOM-free so it can be unit
 * tested in isolation (see `derived.test.ts`). UI components stay thin and
 * just render the structures these helpers produce.
 */

import type { BlastHit, BlastJobSummary } from "@/api/endpoints";
import { numberValue, organismFromStitle, parseLeadingTaxid } from "./helpers";

// ---------------------------------------------------------------------------
// Shared review-status bucketing
// ---------------------------------------------------------------------------

export type ReviewBucket = "strong" | "review" | "low" | "weak" | "unclassified";

const REVIEW_BUCKET: Record<string, ReviewBucket> = {
  strong_match: "strong",
  review_priority: "review",
  low_confidence: "low",
  weak_hit: "weak",
  unclassified: "unclassified",
};

export const REVIEW_BUCKET_COLOR: Record<ReviewBucket, string> = {
  strong: "#3fbf6a",
  review: "#f0c674",
  low: "#e0a24a",
  weak: "#e0524a",
  unclassified: "#7a8290",
};

export const REVIEW_BUCKET_LABEL: Record<ReviewBucket, string> = {
  strong: "Strong match",
  review: "Review priority",
  low: "Low confidence",
  weak: "Weak hit",
  unclassified: "Unclassified",
};

export function reviewBucketOf(hit: BlastHit): ReviewBucket {
  return REVIEW_BUCKET[hit.review_status ?? "unclassified"] ?? "unclassified";
}

/**
 * Query-coverage percent for a hit. Prefers the BLAST `qcovs` column when
 * present; otherwise derives a per-HSP approximation from the aligned query
 * span (`|qend - qstart| + 1 / qlen`). Returns null when neither is usable.
 */
export function hitQueryCoverage(hit: BlastHit): number | null {
  const qcovs = numberValue(hit.qcovs);
  if (qcovs !== null) return clamp(qcovs, 0, 100);
  const qstart = numberValue(hit.qstart);
  const qend = numberValue(hit.qend);
  const qlen = numberValue(hit.qlen);
  if (qstart === null || qend === null || qlen === null || qlen <= 0) return null;
  const span = Math.abs(qend - qstart) + 1;
  return clamp((span / qlen) * 100, 0, 100);
}

function clamp(value: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, value));
}

// ---------------------------------------------------------------------------
// #1 Coverage × Identity triage scatter
// ---------------------------------------------------------------------------

export type TriageQuadrant = "ortholog" | "divergent" | "partial" | "marginal";

export interface TriagePoint {
  hit: BlastHit;
  qcovs: number;
  pident: number;
  bitscore: number;
  bucket: ReviewBucket;
  quadrant: TriageQuadrant;
}

export const TRIAGE_IDENTITY_THRESHOLD = 70;
export const TRIAGE_COVERAGE_THRESHOLD = 50;

export const TRIAGE_QUADRANT_LABEL: Record<TriageQuadrant, string> = {
  ortholog: "High cover · high identity (ortholog candidates)",
  divergent: "High cover · low identity (divergent / distant homologs)",
  partial: "Low cover · high identity (partial / domain matches)",
  marginal: "Low cover · low identity (marginal hits)",
};

function quadrantOf(qcovs: number, pident: number): TriageQuadrant {
  const highCover = qcovs >= TRIAGE_COVERAGE_THRESHOLD;
  const highIdentity = pident >= TRIAGE_IDENTITY_THRESHOLD;
  if (highCover && highIdentity) return "ortholog";
  if (highCover && !highIdentity) return "divergent";
  if (!highCover && highIdentity) return "partial";
  return "marginal";
}

/**
 * Project each hit onto the coverage (x) × identity (y) plane. Hits missing
 * either coordinate are dropped — they cannot be placed honestly. Point size
 * is left to the renderer (driven by `bitscore`).
 */
export function buildTriagePoints(hits: BlastHit[]): TriagePoint[] {
  const points: TriagePoint[] = [];
  for (const hit of hits) {
    const qcovs = hitQueryCoverage(hit);
    const pident = numberValue(hit.pident);
    if (qcovs === null || pident === null) continue;
    const bitscore = numberValue(hit.bitscore) ?? 0;
    points.push({
      hit,
      qcovs,
      pident: clamp(pident, 0, 100),
      bitscore,
      bucket: reviewBucketOf(hit),
      quadrant: quadrantOf(qcovs, pident),
    });
  }
  return points;
}

export function triageQuadrantCounts(
  points: TriagePoint[],
): Record<TriageQuadrant, number> {
  const counts: Record<TriageQuadrant, number> = {
    ortholog: 0,
    divergent: 0,
    partial: 0,
    marginal: 0,
  };
  for (const point of points) counts[point.quadrant] += 1;
  return counts;
}

// ---------------------------------------------------------------------------
// #2 Taxonomic dereplication
// ---------------------------------------------------------------------------

export type DerepRank = "species" | "genus";

export interface TaxonRollupRow {
  key: string;
  label: string;
  taxid: number | null;
  hitCount: number;
  bestHit: BlastHit;
  bestBitscore: number | null;
  bestEvalue: number | null;
  bestIdentity: number | null;
  members: BlastHit[];
}

/** Best scientific name available for a hit (sscinames → stitle heuristic). */
export function hitOrganism(hit: BlastHit): string {
  const sci = (hit.sscinames ?? "").split(";")[0]?.trim();
  if (sci) return sci;
  return organismFromStitle(hit.stitle) || "";
}

function rankKey(organism: string, rank: DerepRank): string {
  if (!organism) return "";
  if (rank === "genus") {
    return organism.split(/\s+/)[0] ?? organism;
  }
  // species: keep the first two tokens ("Genus species"), dropping strain noise.
  return organism.split(/\s+/).slice(0, 2).join(" ");
}

function isBetterHit(candidate: BlastHit, incumbent: BlastHit): boolean {
  const cb = numberValue(candidate.bitscore);
  const ib = numberValue(incumbent.bitscore);
  if (cb !== null && ib !== null && cb !== ib) return cb > ib;
  const ce = numberValue(candidate.evalue);
  const ie = numberValue(incumbent.evalue);
  if (ce !== null && ie !== null && ce !== ie) return ce < ie;
  return false;
}

/**
 * Collapse hits to one row per taxon at the requested rank, keeping the
 * best-scoring representative and the full member list (so the UI can expand
 * "+ N more"). Hits without a resolvable organism are grouped under an
 * "Unassigned" bucket so nothing is silently dropped.
 */
export function derepByRank(hits: BlastHit[], rank: DerepRank): TaxonRollupRow[] {
  const groups = new Map<string, TaxonRollupRow>();
  for (const hit of hits) {
    const organism = hitOrganism(hit);
    const label = rankKey(organism, rank) || "Unassigned";
    const key = label.toLowerCase();
    const existing = groups.get(key);
    if (existing) {
      existing.hitCount += 1;
      existing.members.push(hit);
      if (isBetterHit(hit, existing.bestHit)) {
        existing.bestHit = hit;
        existing.bestBitscore = numberValue(hit.bitscore);
        existing.bestEvalue = numberValue(hit.evalue);
        existing.bestIdentity = numberValue(hit.pident);
        existing.taxid = parseLeadingTaxid(hit.staxids) ?? existing.taxid;
      }
    } else {
      groups.set(key, {
        key,
        label,
        taxid: parseLeadingTaxid(hit.staxids),
        hitCount: 1,
        bestHit: hit,
        bestBitscore: numberValue(hit.bitscore),
        bestEvalue: numberValue(hit.evalue),
        bestIdentity: numberValue(hit.pident),
        members: [hit],
      });
    }
  }
  return [...groups.values()].sort((a, b) => {
    const ab = a.bestBitscore ?? -Infinity;
    const bb = b.bestBitscore ?? -Infinity;
    if (ab !== bb) return bb - ab;
    return b.hitCount - a.hitCount;
  });
}

// ---------------------------------------------------------------------------
// #3 Subject coordinate map (multi-HSP tiling)
// ---------------------------------------------------------------------------

export interface SubjectHsp {
  hit: BlastHit;
  sstart: number;
  send: number;
  qstart: number;
  strand: "plus" | "minus";
  bitscore: number | null;
}

export interface SubjectTrack {
  sseqid: string;
  stitle?: string;
  slen: number;
  hsps: SubjectHsp[];
  hspCount: number;
  /** HSPs map to both strands of the subject → inversion / palindrome signal. */
  hasStrandFlip: boolean;
  /** Subject order disagrees with query order → rearrangement / duplication. */
  hasOrderInversion: boolean;
}

/**
 * Group HSPs by subject and lay them on the subject axis (1..slen). Flags two
 * structural signals a flat hit table hides: mixed strands (`hasStrandFlip`)
 * and a subject coordinate order that disagrees with the query order
 * (`hasOrderInversion`) — both hint at rearrangements, duplications, or
 * repeats worth a closer look.
 */
export function buildSubjectTracks(hits: BlastHit[]): SubjectTrack[] {
  const groups = new Map<string, SubjectTrack>();
  for (const hit of hits) {
    const sstart = numberValue(hit.sstart);
    const send = numberValue(hit.send);
    const qstart = numberValue(hit.qstart);
    if (sstart === null || send === null) continue;
    const hsp: SubjectHsp = {
      hit,
      sstart,
      send,
      qstart: qstart ?? 0,
      strand: send >= sstart ? "plus" : "minus",
      bitscore: numberValue(hit.bitscore),
    };
    const slenFromHit = numberValue(hit.slen);
    const existing = groups.get(hit.sseqid);
    if (existing) {
      existing.hsps.push(hsp);
      existing.slen = Math.max(existing.slen, slenFromHit ?? 0, sstart, send);
    } else {
      groups.set(hit.sseqid, {
        sseqid: hit.sseqid,
        stitle: hit.stitle,
        slen: Math.max(slenFromHit ?? 0, sstart, send),
        hsps: [hsp],
        hspCount: 1,
        hasStrandFlip: false,
        hasOrderInversion: false,
      });
    }
  }
  for (const track of groups.values()) {
    track.hspCount = track.hsps.length;
    track.hasStrandFlip =
      track.hsps.some((h) => h.strand === "plus") &&
      track.hsps.some((h) => h.strand === "minus");
    track.hasOrderInversion = detectOrderInversion(track.hsps);
  }
  return [...groups.values()].sort((a, b) => b.hspCount - a.hspCount);
}

/**
 * True when ordering the HSPs by query start does not follow the subject
 * coordinates in the direction the dominant strand implies — the classic
 * tabular-output fingerprint of an inversion, duplication, or rearrangement.
 *
 * Strand-aware so a collinear antisense (minus-strand) match — whose subject
 * coordinates naturally *decrease* as the query advances — is not mistaken
 * for a rearrangement. Single-HSP subjects are never flagged.
 */
function detectOrderInversion(hsps: SubjectHsp[]): boolean {
  if (hsps.length < 2) return false;
  const plus = hsps.filter((h) => h.strand === "plus").length;
  const expectIncreasing = plus >= hsps.length - plus;
  const byQuery = [...hsps].sort((a, b) => a.qstart - b.qstart);
  for (let i = 1; i < byQuery.length; i += 1) {
    const delta = byQuery[i].sstart - byQuery[i - 1].sstart;
    if (expectIncreasing && delta < 0) return true;
    if (!expectIncreasing && delta > 0) return true;
  }
  return false;
}

// ---------------------------------------------------------------------------
// #4 Plain-language hit evidence
// ---------------------------------------------------------------------------

export type ConfidenceLevel = "high" | "moderate" | "low" | "none";

export interface ConfidenceVerdict {
  level: ConfidenceLevel;
  headline: string;
  detail: string;
}

export const CONFIDENCE_COLOR: Record<ConfidenceLevel, string> = {
  high: "#3fbf6a",
  moderate: "#f0c674",
  low: "#e0a24a",
  none: "#e0524a",
};

/**
 * Translate a raw E-value into a plain-language confidence statement a
 * researcher can paste into a report without re-deriving the math.
 */
export function evalueConfidence(evalue: unknown): ConfidenceVerdict {
  const ev = numberValue(evalue);
  if (ev === null) {
    return {
      level: "none",
      headline: "No E-value",
      detail: "This hit has no reported expect value; treat it as unscored.",
    };
  }
  if (ev <= 1e-50) {
    return {
      level: "high",
      headline: "Essentially certain",
      detail:
        "An alignment this strong is effectively never expected by chance in this search space.",
    };
  }
  if (ev <= 1e-10) {
    return {
      level: "high",
      headline: "Highly significant",
      detail: "Far below the conventional significance cut-off — a confident homolog.",
    };
  }
  if (ev <= 1e-3) {
    return {
      level: "moderate",
      headline: "Significant",
      detail: "Below the usual 0.001 reporting threshold; a credible match worth keeping.",
    };
  }
  if (ev < 1) {
    return {
      level: "low",
      headline: "Marginal",
      detail:
        "Near the noise floor — corroborate with coverage, identity, and a second line of evidence.",
    };
  }
  return {
    level: "none",
    headline: "Likely by chance",
    detail: "An E-value at or above 1 means a hit this good is expected at random.",
  };
}

/** Short note describing the bit score (length-normalised, DB-independent). */
export function bitscoreNote(bitscore: unknown): string {
  const bits = numberValue(bitscore);
  if (bits === null) return "Bit score unavailable.";
  if (bits >= 200) return `${bits.toFixed(0)} bits — strong, database-independent signal.`;
  if (bits >= 80) return `${bits.toFixed(0)} bits — moderate signal; check coverage.`;
  if (bits >= 50) return `${bits.toFixed(0)} bits — weak signal; corroborate.`;
  return `${bits.toFixed(0)} bits — very weak; likely background.`;
}

export interface SearchSpacePin {
  searchSpace: number | null;
  source: string | null;
  text: string;
}

/**
 * Surface the effective BLAST search space (and where it came from) so the
 * E-value above is interpretable and reproducible. Reads the compatibility
 * contract first (the explicit `-searchsp` pin), then the submitted option.
 */
export function searchSpacePin(job: BlastJobSummary | null | undefined): SearchSpacePin {
  const contract = job?.provenance?.compatibility;
  const fromContract = numberValue(contract?.searchsp);
  const source = typeof contract?.search_space_source === "string"
    ? contract.search_space_source
    : null;
  const options = job?.provenance?.options as Record<string, unknown> | undefined;
  const fromOptions = numberValue(
    options?.db_effective_search_space ??
      (job?.payload as Record<string, unknown> | undefined)?.db_effective_search_space,
  );
  const searchSpace = fromContract ?? fromOptions ?? null;
  if (searchSpace === null) {
    return {
      searchSpace: null,
      source,
      text: "Effective search space not pinned for this run.",
    };
  }
  const sourceLabel = source ? ` (source: ${source})` : "";
  return {
    searchSpace,
    source,
    text: `Effective search space ${formatScientific(searchSpace)} letters${sourceLabel}.`,
  };
}

// ---------------------------------------------------------------------------
// #5 Result Passport — Methods text + parity verdict
// ---------------------------------------------------------------------------

export type ParityState = "equivalent" | "drift" | "approximate" | "unknown";

export interface ParityVerdict {
  state: ParityState;
  label: string;
  detail: string;
}

/**
 * Top-of-page parity badge derived from the compatibility contract. Maps the
 * backend's `mode` to a researcher-facing "is this NCBI-equivalent?" verdict.
 */
export function parityVerdict(job: BlastJobSummary | null | undefined): ParityVerdict {
  const contract = job?.provenance?.compatibility;
  if (!contract) {
    return {
      state: "unknown",
      label: "Parity not evaluated",
      detail: "No compatibility contract was recorded for this run.",
    };
  }
  const warnings = contract.warnings?.length ?? 0;
  if (contract.mode === "precise" && contract.eligible) {
    return {
      state: "equivalent",
      label: "NCBI-equivalent",
      detail:
        warnings > 0
          ? `Precise mode with ${warnings} advisory note(s).`
          : "Precise mode: E-values computed against the pinned full-database search space.",
    };
  }
  if (contract.mode === "calibration_required") {
    return {
      state: "drift",
      label: "Search-space drift",
      detail:
        "The database snapshot differs from the calibrated search space; E-values may shift versus NCBI.",
    };
  }
  if (contract.mode === "approximate") {
    return {
      state: "approximate",
      label: "Approximate",
      detail: "Sharded/partitioned search — results approximate the full-database run.",
    };
  }
  return {
    state: "unknown",
    label: "Parity unclear",
    detail: "Compatibility mode was not recognised.",
  };
}

/**
 * Auto-generate a copy-pasteable Methods sentence from the run's provenance
 * bundle. Falls back gracefully field-by-field so a partially-populated
 * bundle still yields a usable sentence.
 */
export function buildMethodsText(job: BlastJobSummary | null | undefined): string {
  const prov = job?.provenance;
  const program = (prov?.blast?.program ?? job?.program ?? "BLAST").toString();
  const version = prov?.blast?.version ? `BLAST+ ${prov.blast.version}` : "BLAST+";
  const db = (job?.db ?? readString(prov?.database, "name") ?? "the target database").toString();
  const snapshot = readString(prov?.database, "snapshot") ?? readString(prov?.database, "update_date");
  const seqs = numberValue(readUnknown(prov?.database, "number_of_sequences"));
  const letters = numberValue(readUnknown(prov?.database, "number_of_letters"));
  const options = (prov?.options ?? {}) as Record<string, unknown>;
  const evalue = numberValue(options.evalue ?? (job?.payload as Record<string, unknown>)?.evalue);
  const pin = searchSpacePin(job);

  const dbDetail: string[] = [];
  if (snapshot) dbDetail.push(`snapshot ${snapshot}`);
  if (seqs !== null) dbDetail.push(`${seqs.toLocaleString()} sequences`);
  if (letters !== null) dbDetail.push(`${formatScientific(letters)} letters`);
  const dbClause = dbDetail.length ? ` (${dbDetail.join(", ")})` : "";

  const parts: string[] = [];
  parts.push(
    `Sequence similarity searches were performed with ${version} ${program} against the ${db} database${dbClause}.`,
  );
  if (evalue !== null) {
    parts.push(`Hits were reported at an E-value threshold of ${formatPlain(evalue)}.`);
  }
  if (pin.searchSpace !== null) {
    parts.push(
      `E-values were computed against an effective search space of ${formatScientific(
        pin.searchSpace,
      )} letters${pin.source ? ` (${pin.source})` : ""}.`,
    );
  }
  const verdict = parityVerdict(job);
  if (verdict.state === "equivalent") {
    parts.push("This configuration is equivalent to a single full-database NCBI BLAST run.");
  } else if (verdict.state === "drift") {
    parts.push(
      "Note: the database snapshot differs from the calibrated search space, so E-values may differ from a contemporaneous NCBI search.",
    );
  } else if (verdict.state === "approximate") {
    parts.push("Note: a sharded search was used; results approximate a full-database run.");
  }
  return parts.join(" ");
}

// ---------------------------------------------------------------------------
// small formatting helpers (local — not exported to avoid helpers.ts churn)
// ---------------------------------------------------------------------------

function readString(
  record: Record<string, unknown> | undefined,
  key: string,
): string | null {
  const value = record?.[key];
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function readUnknown(
  record: Record<string, unknown> | undefined,
  key: string,
): unknown {
  return record?.[key];
}

export function formatScientific(value: number): string {
  if (!Number.isFinite(value)) return "—";
  if (value === 0) return "0";
  if (Math.abs(value) >= 1e5 || Math.abs(value) < 1e-3) {
    return value.toExponential(2);
  }
  return value.toLocaleString();
}

function formatPlain(value: number): string {
  if (!Number.isFinite(value)) return "—";
  if (value !== 0 && (Math.abs(value) < 1e-3 || Math.abs(value) >= 1e5)) {
    return value.toExponential(1);
  }
  return String(value);
}
