/**
 * Tests for sequence/sequenceAnalysis.ts.
 *
 * Responsibility: Lock the molecular-diagnostics analytics contract —
 * composition/GC, ambiguous/N counts, reverse complement, sub-range extraction
 * and FASTA, assembly-gap inventory, hit×gap relation, and containing-feature
 * detection — that the Sequence Detail analysis card renders against.
 * Edit boundaries: Pure-function assertions only; 1-based inclusive coords.
 * Key entry points: the `describe` blocks below.
 * Risky contracts: coordinate base (1-based inclusive) must match the UI.
 * Validation: `cd web && npm test -- sequenceAnalysis.test`.
 */
import { describe, expect, it } from "vitest";

import type { NuccoreFeature } from "@/api/ncbi";
import {
  baseComposition,
  collectAssemblyGaps,
  extractSubrange,
  featuresOverlappingRange,
  gapSummary,
  hitGapRelation,
  isFinishedAssembly,
  rangesOverlap,
  reverseComplement,
  subrangeFasta,
} from "./sequenceAnalysis";

function feature(
  key: string,
  start: number,
  stop: number,
  qualifiers: { name: string; value: string }[] = [],
): NuccoreFeature {
  return {
    key,
    location: `${start}..${stop}`,
    intervals: [{ start, stop, point: null, accession: null }],
    qualifiers,
  };
}

describe("baseComposition", () => {
  it("counts bases and computes GC over unambiguous denominator", () => {
    const comp = baseComposition("ACGTACGT");
    expect(comp.counts.A).toBe(2);
    expect(comp.counts.G).toBe(2);
    expect(comp.gc).toBeCloseTo(0.5, 5);
    expect(comp.hasUncertain).toBe(false);
  });

  it("separates N from non-N ambiguous codes and excludes them from GC", () => {
    const comp = baseComposition("GGCCNNRY");
    expect(comp.n).toBe(2);
    expect(comp.ambiguous).toBe(2);
    expect(comp.gc).toBeCloseTo(1, 5); // 4 G/C over 4 unambiguous
    expect(comp.hasUncertain).toBe(true);
  });

  it("returns null GC when there are no unambiguous bases", () => {
    expect(baseComposition("NNNN").gc).toBeNull();
  });
});

describe("reverseComplement", () => {
  it("reverse-complements and preserves case + IUPAC codes", () => {
    expect(reverseComplement("ACGT")).toBe("ACGT");
    expect(reverseComplement("AAAC")).toBe("GTTT");
    expect(reverseComplement("acgtN")).toBe("Nacgt");
    expect(reverseComplement("R")).toBe("Y");
  });
});

describe("extractSubrange", () => {
  const seq = "ACGTACGTAC"; // 1..10
  it("extracts a 1-based inclusive window", () => {
    expect(extractSubrange(seq, 1, 4)).toBe("ACGT");
    expect(extractSubrange(seq, 5, 5)).toBe("A");
  });
  it("clamps and orders out-of-bounds / reversed input", () => {
    expect(extractSubrange(seq, 8, 100)).toBe("TAC");
    expect(extractSubrange(seq, 4, 1)).toBe("ACGT");
  });
  it("returns empty for invalid input", () => {
    expect(extractSubrange(seq, NaN, 4)).toBe("");
  });
});

describe("subrangeFasta", () => {
  it("emits a coordinate-annotated defline and wrapped body", () => {
    const fa = subrangeFasta("OZ254605.1", "ACGTACGTAC", 2, 5);
    expect(fa.split("\n")[0]).toBe(">OZ254605.1:2-5");
    expect(fa.split("\n")[1]).toBe("CGTA");
  });
  it("flags reverse-complement in the defline", () => {
    const fa = subrangeFasta("X", "AAAC", 1, 4, { reverseComplement: true });
    expect(fa.split("\n")[0]).toBe(">X:1-4 reverse-complement");
    expect(fa.split("\n")[1]).toBe("GTTT");
  });
});

describe("assembly gaps", () => {
  const features = [
    feature("source", 1, 1000),
    feature("assembly_gap", 100, 200),
    feature("assembly_gap", 500, 510),
    feature("CDS", 300, 400, [{ name: "gene", value: "F3L" }]),
  ];

  it("collects gap intervals", () => {
    const gaps = collectAssemblyGaps(features);
    expect(gaps).toHaveLength(2);
    expect(gaps[0]).toEqual({ start: 100, stop: 200, length: 101 });
  });

  it("summarises count / total / fraction", () => {
    const gaps = collectAssemblyGaps(features);
    const sum = gapSummary(gaps, 1000);
    expect(sum.count).toBe(2);
    expect(sum.totalBp).toBe(101 + 11);
    expect(sum.fraction).toBeCloseTo(0.112, 3);
  });

  it("isFinishedAssembly is false when gaps exist", () => {
    expect(isFinishedAssembly(collectAssemblyGaps(features))).toBe(false);
    expect(isFinishedAssembly([])).toBe(true);
  });
});

describe("rangesOverlap", () => {
  it("detects overlap and separation", () => {
    expect(rangesOverlap(10, 20, 15, 25)).toBe(true);
    expect(rangesOverlap(10, 20, 21, 30)).toBe(false);
    expect(rangesOverlap(10, 20, 20, 30)).toBe(true);
  });
});

describe("hitGapRelation", () => {
  const gaps = collectAssemblyGaps([
    feature("assembly_gap", 100, 200),
    feature("assembly_gap", 500, 510),
  ]);

  it("flags overlap with a gap", () => {
    const rel = hitGapRelation({ start: 150, stop: 250 }, gaps);
    expect(rel.kind).toBe("overlap");
    expect(rel.overlapping).toHaveLength(1);
  });

  it("flags adjacency within the flank window", () => {
    const rel = hitGapRelation({ start: 210, stop: 240 }, gaps, 50);
    expect(rel.kind).toBe("adjacent");
    expect(rel.nearestDistance).toBe(10);
  });

  it("reports clear when far from any gap", () => {
    const rel = hitGapRelation({ start: 300, stop: 400 }, gaps, 50);
    expect(rel.kind).toBe("clear");
  });
});

describe("featuresOverlappingRange", () => {
  const features = [
    feature("source", 1, 1000),
    feature("assembly_gap", 100, 200),
    feature("gene", 250, 800, [{ name: "gene", value: "F3L" }]),
    feature("CDS", 300, 400, [{ name: "product", value: "F3 protein" }]),
  ];

  it("returns annotated features overlapping the range, most specific first", () => {
    const hits = featuresOverlappingRange(features, { start: 350, stop: 360 });
    expect(hits.map((f) => f.key)).toEqual(["CDS", "gene"]);
  });

  it("excludes source and gap features", () => {
    const hits = featuresOverlappingRange(features, { start: 150, stop: 160 });
    expect(hits).toHaveLength(0);
  });
});
