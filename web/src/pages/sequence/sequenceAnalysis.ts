/**
 * Pure sequence + feature analytics for the Sequence Detail page.
 *
 * Responsibility: Turn a resolved FASTA residue string and a record's GenBank
 * features into the molecular-diagnostics signals a researcher needs to judge
 * whether a BLAST hit window is a usable assay target — base composition and
 * GC%, ambiguous/N base counts, assembly-gap inventory and hit×gap overlap,
 * reverse complement, sub-range FASTA extraction, and the annotated features a
 * hit range falls inside. All functions are deterministic and free of React /
 * DOM / network so the contract is locked by `sequenceAnalysis.test.ts`.
 * Edit boundaries: Pure string/number transforms only. Coordinates are 1-based
 * inclusive everywhere (matching GenBank ORIGIN and the `hl_start`/`hl_stop`
 * subject coordinates the hits table emits). No UI strings here.
 * Key entry points: `baseComposition`, `reverseComplement`, `extractSubrange`,
 * `subrangeFasta`, `collectAssemblyGaps`, `gapSummary`, `rangesOverlap`,
 * `hitGapRelation`, `featuresOverlappingRange`.
 * Risky contracts: `TargetAnalysisCard.tsx` and `SequenceBlocks.tsx` render
 * directly off these shapes; the IUPAC tables map 1:1 to the nucleotide colour
 * legend.
 * Validation: `cd web && npm test -- sequenceAnalysis.test`.
 */

import type { NuccoreFeature } from "@/api/ncbi";

/** Unambiguous DNA/RNA bases that get a composition bucket of their own. */
export const CANONICAL_BASES = ["A", "C", "G", "T", "U"] as const;

// IUPAC degenerate nucleotide codes (excluding N, which we count separately
// because a run of Ns usually means a sequencing/assembly gap rather than a
// designed degeneracy). Primers/probes should avoid these positions.
export const IUPAC_AMBIGUOUS = new Set([
  "R",
  "Y",
  "S",
  "W",
  "K",
  "M",
  "B",
  "D",
  "H",
  "V",
]);

const COMPLEMENT: Record<string, string> = {
  A: "T",
  T: "A",
  U: "A",
  G: "C",
  C: "G",
  N: "N",
  R: "Y",
  Y: "R",
  S: "S",
  W: "W",
  K: "M",
  M: "K",
  B: "V",
  V: "B",
  D: "H",
  H: "D",
  "-": "-",
};

export interface BaseComposition {
  length: number;
  /** Per-canonical-base counts (uppercased, U folded separately). */
  counts: Record<string, number>;
  /** Count of `N` residues (hard-masked / unknown). */
  n: number;
  /** Count of non-N IUPAC degenerate codes (R/Y/W/S/K/M/B/D/H/V). */
  ambiguous: number;
  /** GC fraction over A/C/G/T(/U) only (Ns and ambiguous excluded), or null. */
  gc: number | null;
  /** True when the window holds any N or non-N ambiguous residue. */
  hasUncertain: boolean;
}

/**
 * Tally base composition over a residue string. GC% is computed over the
 * unambiguous A/C/G/T(/U) denominator so a window padded with Ns does not
 * silently deflate the reported GC content.
 */
export function baseComposition(seq: string): BaseComposition {
  const counts: Record<string, number> = { A: 0, C: 0, G: 0, T: 0, U: 0 };
  let n = 0;
  let ambiguous = 0;
  for (const raw of seq) {
    const ch = raw.toUpperCase();
    if (ch === "N") {
      n += 1;
    } else if (ch in counts) {
      counts[ch] += 1;
    } else if (IUPAC_AMBIGUOUS.has(ch)) {
      ambiguous += 1;
    }
  }
  const gcDenom = counts.A + counts.C + counts.G + counts.T + counts.U;
  const gc = gcDenom > 0 ? (counts.G + counts.C) / gcDenom : null;
  return {
    length: seq.length,
    counts,
    n,
    ambiguous,
    gc,
    hasUncertain: n > 0 || ambiguous > 0,
  };
}

/** Reverse-complement a residue string, preserving IUPAC codes and case. */
export function reverseComplement(seq: string): string {
  let out = "";
  for (let i = seq.length - 1; i >= 0; i -= 1) {
    const ch = seq[i];
    const upper = ch.toUpperCase();
    const comp = COMPLEMENT[upper] ?? "N";
    out += ch === upper ? comp : comp.toLowerCase();
  }
  return out;
}

/**
 * Extract a 1-based inclusive sub-range from a residue string. Returns "" when
 * the range is invalid or entirely outside the sequence; clamps a partially
 * out-of-bounds range to the available residues.
 */
export function extractSubrange(seq: string, start: number, stop: number): string {
  if (!Number.isFinite(start) || !Number.isFinite(stop)) return "";
  const lo = Math.max(1, Math.min(start, stop));
  const hi = Math.min(seq.length, Math.max(start, stop));
  if (hi < lo) return "";
  return seq.slice(lo - 1, hi);
}

/** Build a FASTA record for a sub-range with a coordinate-annotated defline. */
export function subrangeFasta(
  accession: string,
  seq: string,
  start: number,
  stop: number,
  opts?: { reverseComplement?: boolean },
): string {
  const lo = Math.max(1, Math.min(start, stop));
  const hi = Math.max(start, stop);
  let body = extractSubrange(seq, start, stop);
  const rc = opts?.reverseComplement ?? false;
  if (rc) body = reverseComplement(body);
  const strand = rc ? " reverse-complement" : "";
  const header = `>${accession}:${lo}-${hi}${strand}`;
  const wrapped = body.match(/.{1,70}/g) ?? [];
  return [header, ...wrapped].join("\n");
}

export interface AssemblyGap {
  start: number;
  stop: number;
  length: number;
}

/** Collect every `assembly_gap` (and `gap`) feature as 1-based intervals. */
export function collectAssemblyGaps(features: NuccoreFeature[]): AssemblyGap[] {
  const gaps: AssemblyGap[] = [];
  for (const feature of features) {
    if (feature.key !== "assembly_gap" && feature.key !== "gap") continue;
    for (const interval of feature.intervals) {
      if (interval.start != null && interval.stop != null) {
        const start = Math.min(interval.start, interval.stop);
        const stop = Math.max(interval.start, interval.stop);
        gaps.push({ start, stop, length: stop - start + 1 });
      }
    }
  }
  return gaps;
}

export interface GapSummary {
  count: number;
  totalBp: number;
  /** Gap span as a fraction of the whole sequence length, or null. */
  fraction: number | null;
}

export function gapSummary(
  gaps: AssemblyGap[],
  totalLength: number | null | undefined,
): GapSummary {
  const totalBp = gaps.reduce((acc, g) => acc + g.length, 0);
  const fraction =
    totalLength && totalLength > 0 ? totalBp / totalLength : null;
  return { count: gaps.length, totalBp, fraction };
}

/** Inclusive 1-based interval overlap test. */
export function rangesOverlap(
  aStart: number,
  aStop: number,
  bStart: number,
  bStop: number,
): boolean {
  return aStart <= bStop && bStart <= aStop;
}

export type HitGapRelationKind = "overlap" | "adjacent" | "clear";

export interface HitGapRelation {
  kind: HitGapRelationKind;
  /** Gaps that overlap the hit window (kind === "overlap"). */
  overlapping: AssemblyGap[];
  /** Nearest gap within `adjacencyBp` of the window (kind === "adjacent"). */
  nearest: AssemblyGap | null;
  /** Distance in bp to `nearest` when adjacent. */
  nearestDistance: number | null;
}

/**
 * Classify how a hit window relates to the record's assembly gaps. A hit that
 * overlaps an N-gap is worthless as an assay target; one that sits within a few
 * bases of a gap edge is risky. `adjacencyBp` defaults to 50 (a typical
 * primer's worth of flank).
 */
export function hitGapRelation(
  hit: { start: number; stop: number },
  gaps: AssemblyGap[],
  adjacencyBp = 50,
): HitGapRelation {
  const lo = Math.min(hit.start, hit.stop);
  const hi = Math.max(hit.start, hit.stop);
  const overlapping = gaps.filter((g) => rangesOverlap(lo, hi, g.start, g.stop));
  if (overlapping.length > 0) {
    return { kind: "overlap", overlapping, nearest: null, nearestDistance: null };
  }
  let nearest: AssemblyGap | null = null;
  let nearestDistance: number | null = null;
  for (const g of gaps) {
    const distance = g.stop < lo ? lo - g.stop : g.start > hi ? g.start - hi : 0;
    if (nearestDistance == null || distance < nearestDistance) {
      nearestDistance = distance;
      nearest = g;
    }
  }
  if (nearest != null && nearestDistance != null && nearestDistance <= adjacencyBp) {
    return { kind: "adjacent", overlapping: [], nearest, nearestDistance };
  }
  return { kind: "clear", overlapping: [], nearest: null, nearestDistance: null };
}

/** First concrete 1-based interval of a feature, or null. */
export function featureInterval(
  feature: NuccoreFeature,
): { start: number; stop: number } | null {
  for (const interval of feature.intervals) {
    if (interval.start != null && interval.stop != null) {
      return {
        start: Math.min(interval.start, interval.stop),
        stop: Math.max(interval.start, interval.stop),
      };
    }
  }
  return null;
}

// Feature keys that carry no biological annotation value when answering "what
// gene does my hit fall in" — skip them so the containing-feature list stays
// meaningful.
const NON_ANNOTATION_KEYS = new Set(["source", "assembly_gap", "gap"]);

/**
 * Annotated features whose interval overlaps the given range, nearest/most
 * specific first (shortest span wins). Excludes `source` and gap features.
 */
export function featuresOverlappingRange(
  features: NuccoreFeature[],
  range: { start: number; stop: number },
): NuccoreFeature[] {
  const lo = Math.min(range.start, range.stop);
  const hi = Math.max(range.start, range.stop);
  const hits: { feature: NuccoreFeature; span: number }[] = [];
  for (const feature of features) {
    if (feature.key && NON_ANNOTATION_KEYS.has(feature.key)) continue;
    const iv = featureInterval(feature);
    if (!iv) continue;
    if (rangesOverlap(lo, hi, iv.start, iv.stop)) {
      hits.push({ feature, span: iv.stop - iv.start });
    }
  }
  hits.sort((a, b) => a.span - b.span);
  return hits.map((h) => h.feature);
}

/** True when the record looks like a finished assembly (no N-gaps reported). */
export function isFinishedAssembly(gaps: AssemblyGap[]): boolean {
  return gaps.length === 0;
}
