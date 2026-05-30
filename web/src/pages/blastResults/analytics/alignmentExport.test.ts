import { describe, expect, it } from "vitest";

import {
  buildAlignmentExportFilename,
  buildAlignmentFasta,
  buildPairwiseAlignmentText,
  wrapFasta,
} from "./alignmentExport";

import type { BlastHit } from "@/api/endpoints";

/**
 * These helpers used to live inside `AlignmentViewer.tsx`. They are
 * pure (no React, no DOM) so we exercise them directly here; the
 * render-layer behaviour stays in the component's own test files.
 */

function makeHit(overrides: Partial<BlastHit> = {}): BlastHit {
  return {
    qseqid: "Query_1",
    sseqid: "AB123456.1",
    pident: 98.5,
    length: 17,
    mismatch: 1,
    gapopen: 0,
    qstart: 1,
    qend: 16,
    sstart: 101,
    send: 116,
    evalue: 1e-30,
    bitscore: 250.0,
    qlen: 60,
    slen: 600,
    qseq: "ACGTACGT-ACGTACGT",
    sseq: "ACGTACGTACGTACGT-",
    stitle: "Escherichia coli plasmid p1, complete sequence",
    ...overrides,
  } as BlastHit;
}

describe("wrapFasta", () => {
  it("returns empty string for empty input", () => {
    expect(wrapFasta("")).toBe("");
  });

  it("wraps at the default width of 70 characters", () => {
    const seq = "A".repeat(150);
    const wrapped = wrapFasta(seq);
    const lines = wrapped.split("\n");
    expect(lines).toHaveLength(3);
    expect(lines[0]).toHaveLength(70);
    expect(lines[1]).toHaveLength(70);
    expect(lines[2]).toHaveLength(10);
  });

  it("respects a custom width", () => {
    const seq = "ACGT".repeat(10); // 40 chars
    const wrapped = wrapFasta(seq, 10);
    const lines = wrapped.split("\n");
    expect(lines).toHaveLength(4);
    expect(lines.every((line) => line.length <= 10)).toBe(true);
  });
});

describe("buildAlignmentFasta", () => {
  it("emits two FASTA records and strips gap characters", () => {
    const fasta = buildAlignmentFasta(makeHit());
    const lines = fasta.split("\n");

    expect(lines[0]).toBe(">Query_1 aligned_region=1-16");
    expect(lines[1]).toBe("ACGTACGTACGTACGT");
    expect(lines[2]).toBe(
      ">AB123456.1 Escherichia coli plasmid p1, complete sequence aligned_region=101-116",
    );
    expect(lines[3]).toBe("ACGTACGTACGTACGT");
    expect(lines[lines.length - 1]).toBe("");
    // gap characters never leak into the sequence lines
    expect(lines[1]).not.toContain("-");
    expect(lines[3]).not.toContain("-");
  });

  it("falls back to generic identifiers when fields are missing", () => {
    const fasta = buildAlignmentFasta(
      makeHit({
        qseqid: undefined,
        sseqid: undefined,
        stitle: undefined,
      } as Partial<BlastHit>),
    );
    expect(fasta).toContain(">query aligned_region=");
    expect(fasta).toContain(">subject aligned_region=");
  });

  it("wraps long sequences at 70 characters per line", () => {
    const longSeq = "A".repeat(150);
    const fasta = buildAlignmentFasta(makeHit({ qseq: longSeq, sseq: longSeq }));
    const lines = fasta.split("\n");
    expect(lines[1]).toHaveLength(70);
    expect(lines[2]).toHaveLength(70);
    expect(lines[3]).toHaveLength(10);
  });
});

describe("buildPairwiseAlignmentText", () => {
  it("emits NCBI-style Query/Sbjct header + block layout", () => {
    const text = buildPairwiseAlignmentText(makeHit());
    // formatRange uses an en-dash (–) between the two positions
    expect(text).toContain("Query  Query_1  1\u201316 / 60");
    expect(text).toContain("Sbjct  AB123456.1  101\u2013116 / 600");
    expect(text).toContain("Score  250.0 bits");
    expect(text).toContain("Query       1  ACGTACGT-ACGTACGT");
    expect(text).toContain("Sbjct     101  ACGTACGTACGTACGT-");
    // match line: 8 identities, gap, 7 mismatches, gap
    expect(text).toContain("|||||||| ::::::: ");
  });

  it("splits long alignments into 60-character blocks with advancing counters", () => {
    const qseq = "A".repeat(75);
    const sseq = "T".repeat(75); // every column is a mismatch
    const text = buildPairwiseAlignmentText(
      makeHit({
        qseq,
        sseq,
        qstart: 1,
        qend: 75,
        sstart: 201,
        send: 275,
        qlen: 75,
        slen: 1000,
      }),
    );
    // first block starts at column 1 / 201
    expect(text).toContain("Query       1  " + "A".repeat(60));
    expect(text).toContain("Sbjct     201  " + "T".repeat(60));
    // second block starts at column 61 / 261 (advances by 60 columns)
    expect(text).toContain("Query      61  " + "A".repeat(15));
    expect(text).toContain("Sbjct     261  " + "T".repeat(15));
  });

  it("renders ':' for mismatches and ' ' for gaps in the match line", () => {
    const text = buildPairwiseAlignmentText(
      makeHit({
        qseq: "AC-T",
        sseq: "AGGT",
        qstart: 1,
        qend: 3,
        sstart: 1,
        send: 4,
      }),
    );
    // identity, mismatch (C vs G), gap (- vs G), identity
    expect(text).toContain("                |: |");
  });
});

describe("buildAlignmentExportFilename", () => {
  it("uses qseqid__sseqid.fasta when both identifiers are filesystem-safe", () => {
    expect(buildAlignmentExportFilename(makeHit())).toBe("Query_1__AB123456.1.fasta");
  });

  it("replaces unsafe characters with underscores", () => {
    const filename = buildAlignmentExportFilename(
      makeHit({
        qseqid: "query|gi/123 abc",
        sseqid: "ref|XP_001.1|extra:tag",
      }),
    );
    expect(filename).toBe("query_gi_123_abc__ref_XP_001.1_extra_tag.fasta");
  });

  it("falls back to generic names when identifiers are missing", () => {
    const filename = buildAlignmentExportFilename(
      makeHit({ qseqid: undefined, sseqid: undefined } as Partial<BlastHit>),
    );
    expect(filename).toBe("query__subject.fasta");
  });
});
