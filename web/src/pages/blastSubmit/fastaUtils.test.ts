import { describe, expect, it } from "vitest";

import {
  baseComposition,
  deduplicateFasta,
  findHairpin,
  findSelfDimer,
  gcContent,
  hasAmbiguousBases,
  longestGcRun,
  looksLikeNucleotide,
  meltingTemperatureC,
  parseFasta,
  primerDiagnostics,
  reverseComplement,
  reverseComplementFasta,
  serializeFasta,
} from "@/pages/blastSubmit/fastaUtils";

describe("parseFasta", () => {
  it("parses single-record FASTA", () => {
    const records = parseFasta(">q1 desc\nATGC\nGGGG");
    expect(records).toEqual([{ header: "q1 desc", sequence: "ATGCGGGG" }]);
  });

  it("parses multi-record FASTA and skips blank lines", () => {
    const text = ">a\nATCG\n\n>b\nGGGG\nTTTT\n";
    const records = parseFasta(text);
    expect(records).toHaveLength(2);
    expect(records[0]).toEqual({ header: "a", sequence: "ATCG" });
    expect(records[1]).toEqual({ header: "b", sequence: "GGGGTTTT" });
  });

  it("drops empty records", () => {
    const records = parseFasta(">empty\n\n>real\nAAAA");
    expect(records).toEqual([{ header: "real", sequence: "AAAA" }]);
  });
});

describe("serializeFasta", () => {
  it("wraps sequences at the requested line width", () => {
    const text = serializeFasta([{ header: "x", sequence: "ATGCATGC" }], 4);
    expect(text).toBe(">x\nATGC\nATGC");
  });
});

describe("reverseComplement", () => {
  it("reverse-complements unambiguous DNA", () => {
    expect(reverseComplement("ATGC")).toBe("GCAT");
    expect(reverseComplement("AAAATTTT")).toBe("AAAATTTT");
  });

  it("handles IUPAC ambiguity codes", () => {
    // R(A/G) ↔ Y(C/T), W↔W, S↔S, K(G/T)↔M(A/C)
    expect(reverseComplement("RYWSKM")).toBe("KMSWRY");
  });

  it("preserves lowercase as uppercase complement", () => {
    expect(reverseComplement("atgc")).toBe("GCAT");
  });

  it("treats unknown characters as opaque", () => {
    expect(reverseComplement("ATX?CG")).toBe("CG?XAT");
  });
});

describe("reverseComplementFasta", () => {
  it("flips every record and tags the header", () => {
    const input = ">a\nATGC\n>b\nGGGG";
    const output = reverseComplementFasta(input);
    const records = parseFasta(output);
    expect(records).toHaveLength(2);
    expect(records[0].sequence).toBe("GCAT");
    expect(records[0].header).toContain("reverse_complement");
    expect(records[1].sequence).toBe("CCCC");
  });
});

describe("baseComposition / gcContent", () => {
  it("computes GC% over total nt", () => {
    expect(gcContent("ATGCATGC")).toBe(50);
    expect(gcContent("GGGGGGGG")).toBe(100);
    expect(gcContent("AAAAAAAA")).toBe(0);
  });

  it("counts ambiguous bases separately from N", () => {
    const stats = baseComposition("ATGCNRYW");
    expect(stats.length).toBe(8);
    // R, Y, W are IUPAC ambiguity codes → ambiguous = 3
    // N is separate → nCount = 1
    expect(stats.ambiguous).toBe(3);
    expect(stats.nCount).toBe(1);
  });

  it("handles empty input", () => {
    expect(gcContent("")).toBe(0);
    const stats = baseComposition("");
    expect(stats.length).toBe(0);
    expect(stats.composition).toEqual({});
  });
});

describe("hasAmbiguousBases", () => {
  it("flags IUPAC codes but not plain N/A/T/G/C", () => {
    expect(hasAmbiguousBases("ATGCNN")).toBe(false);
    expect(hasAmbiguousBases("ATGCR")).toBe(true);
    expect(hasAmbiguousBases("ATGCY")).toBe(true);
  });
});

describe("looksLikeNucleotide", () => {
  it("accepts pure DNA", () => {
    expect(looksLikeNucleotide("ATGCATGC")).toBe(true);
  });

  it("rejects protein", () => {
    expect(looksLikeNucleotide("MKILVPQHRFLSF")).toBe(false);
  });

  it("rejects empty input", () => {
    expect(looksLikeNucleotide("")).toBe(false);
  });
});

describe("deduplicateFasta", () => {
  it("removes exact duplicate sequences", () => {
    const input = ">a\nATGC\n>b\nATGC\n>c\nGGGG";
    const result = deduplicateFasta(input);
    expect(result.removed).toBe(1);
    expect(result.kept).toBe(2);
    const records = parseFasta(result.text);
    expect(records).toHaveLength(2);
    expect(records[0].sequence).toBe("ATGC");
    expect(records[0].header).toContain("alias=b");
  });

  it("is case-insensitive and whitespace-tolerant", () => {
    const input = ">a\natgc\n>b\nAT GC\n";
    const result = deduplicateFasta(input);
    expect(result.removed).toBe(1);
    expect(result.kept).toBe(1);
  });

  it("leaves unique sequences alone", () => {
    const result = deduplicateFasta(">a\nATGC\n>b\nGGGG\n>c\nTTTT");
    expect(result.removed).toBe(0);
    expect(result.kept).toBe(3);
  });
});

describe("meltingTemperatureC", () => {
  it("uses the Wallace rule for short oligos (≤ 13 nt)", () => {
    // 2*(A+T) + 4*(G+C) — 4-mer ATGC = 2*2 + 4*2 = 12
    expect(meltingTemperatureC("ATGC")).toBe(12);
    // 12-mer 6A+6G = 2*6 + 4*6 = 36
    expect(meltingTemperatureC("AAAAAAGGGGGG")).toBe(36);
  });

  it("uses the salt-adjusted GC formula for 14–60 nt oligos", () => {
    // 20-mer with 10 GC = 64.9 + 41*(10 - 16.4)/20 = 64.9 + 41*(-6.4)/20 = 64.9 - 13.12 = 51.78
    const seq = "ATGCATGCATGCATGCATGC"; // 20 nt, GC=10
    const tm = meltingTemperatureC(seq);
    expect(tm).not.toBeNull();
    expect(tm!).toBeCloseTo(51.78, 1);
  });

  it("returns null for sequences > 60 nt", () => {
    expect(meltingTemperatureC("A".repeat(61))).toBeNull();
  });

  it("returns null for ambiguous / non-ACGTU letters", () => {
    expect(meltingTemperatureC("ATGCN")).toBeNull();
    expect(meltingTemperatureC("ATGCR")).toBeNull();
  });

  it("returns null for empty input", () => {
    expect(meltingTemperatureC("")).toBeNull();
    expect(meltingTemperatureC("   ")).toBeNull();
  });
});

describe("longestGcRun", () => {
  it("returns 0 for empty / pure AT", () => {
    expect(longestGcRun("")).toBe(0);
    expect(longestGcRun("ATATAT")).toBe(0);
  });

  it("counts consecutive G/C runs case-insensitively", () => {
    expect(longestGcRun("ATGGCCTA")).toBe(4);
    expect(longestGcRun("atggccta")).toBe(4);
  });

  it("ignores whitespace (counts across line breaks)", () => {
    // FASTA may wrap lines; whitespace must not reset the run.
    expect(longestGcRun("GG GG")).toBe(4);
    expect(longestGcRun("GG\nGG")).toBe(4);
  });
});

describe("findHairpin", () => {
  it("detects a self-complementary stem", () => {
    // GCGC...GCGC palindromic edges with a 4-nt loop
    const found = findHairpin("GCGCAAAAGCGC", 4);
    expect(found).not.toBeNull();
    expect(found!.length).toBeGreaterThanOrEqual(4);
  });

  it("returns null when no stem ≥ minStem is present", () => {
    expect(findHairpin("AAAAAAAAAA", 4)).toBeNull();
  });

  it("returns null for very long sequences (cap)", () => {
    expect(findHairpin("A".repeat(201))).toBeNull();
  });
});

describe("findSelfDimer", () => {
  it("flags 3′-complementary overlap", () => {
    // Sequence with its 3′ end complementary to its 5′ end: GCGCAA / TTGCGC
    expect(findSelfDimer("GCGCAATTGCGC", 4)).toBeGreaterThanOrEqual(4);
  });

  it("returns 0 for sequences with no notable self-complement", () => {
    expect(findSelfDimer("AAAAAAAAAAAAAA", 4)).toBe(0);
  });

  it("caps work at 200 nt (long input is truncated, not rejected)", () => {
    // Cap is a perf guard, not a validator — long input still returns a value.
    expect(findSelfDimer("A".repeat(250), 4)).toBe(0);
  });
});

describe("primerDiagnostics", () => {
  it("returns full diagnostics for a typical short oligo", () => {
    const d = primerDiagnostics("ATGCATGCATGCATGCATGC"); // 20-mer
    expect(d).not.toBeNull();
    expect(d!.tm).toBeCloseTo(51.78, 1);
    expect(d!.gc).toBeCloseTo(50, 5);
    expect(d!.gcRun).toBeGreaterThanOrEqual(1);
    expect(d!.hairpinLength).toBeGreaterThanOrEqual(0);
    expect(d!.selfDimerLength).toBeGreaterThanOrEqual(0);
  });

  it("returns null for empty input", () => {
    expect(primerDiagnostics("")).toBeNull();
  });

  it("returns null for protein / non-nucleotide input", () => {
    // Common protein letters that fail the nucleotide sanity check.
    expect(primerDiagnostics("MEEPQSDPSVEPPLSQETFSDLWKLLPENN")).toBeNull();
  });

  it("returns null for sequences > 200 nt", () => {
    expect(primerDiagnostics("ATGC".repeat(60))).toBeNull();
  });
});
