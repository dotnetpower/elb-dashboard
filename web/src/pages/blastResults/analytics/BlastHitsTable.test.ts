import { describe, expect, it } from "vitest";

import type { BlastHit } from "@/api/endpoints";

import { buildSubjectAggregates } from "./BlastHitsTable";

const baseHit: BlastHit = {
  qseqid: "queryA",
  sseqid: "NC_001",
  pident: 99.5,
  length: 150,
  mismatch: 1,
  gapopen: 0,
  qstart: 1,
  qend: 150,
  sstart: 100,
  send: 249,
  evalue: 1e-50,
  bitscore: 289,
};

describe("buildSubjectAggregates", () => {
  it("returns an empty map for an empty hit list", () => {
    expect(buildSubjectAggregates([]).size).toBe(0);
  });

  it("treats a single HSP as max == total, hsp_count == 1", () => {
    const map = buildSubjectAggregates([baseHit]);
    expect(map.size).toBe(1);
    const agg = map.get("NC_001")!;
    expect(agg.maxBitscore).toBe(289);
    expect(agg.totalBitscore).toBe(289);
    expect(agg.hspCount).toBe(1);
  });

  it("sums total bitscore and tracks max across multiple HSPs", () => {
    const hits: BlastHit[] = [
      baseHit,
      { ...baseHit, bitscore: 200, qstart: 200, sstart: 260 },
      { ...baseHit, sseqid: "NC_002", bitscore: 250 },
    ];
    const map = buildSubjectAggregates(hits);
    expect(map.get("NC_001")).toEqual({
      maxBitscore: 289,
      totalBitscore: 489,
      hspCount: 2,
    });
    expect(map.get("NC_002")).toEqual({
      maxBitscore: 250,
      totalBitscore: 250,
      hspCount: 1,
    });
  });

  it("tolerates string bitscore values from `-outfmt 6` parsing", () => {
    const hits: BlastHit[] = [
      { ...baseHit, bitscore: "289" },
      { ...baseHit, bitscore: "150", qstart: 200, sstart: 260 },
    ];
    const map = buildSubjectAggregates(hits);
    const agg = map.get("NC_001")!;
    expect(agg.maxBitscore).toBe(289);
    expect(agg.totalBitscore).toBe(439);
    expect(agg.hspCount).toBe(2);
  });

  it("treats non-numeric bitscore as 0 (defensive)", () => {
    const hits: BlastHit[] = [
      { ...baseHit, bitscore: "not-a-number" },
      { ...baseHit, bitscore: 289, qstart: 200, sstart: 260 },
    ];
    const map = buildSubjectAggregates(hits);
    const agg = map.get("NC_001")!;
    expect(agg.maxBitscore).toBe(289);
    expect(agg.totalBitscore).toBe(289);
    expect(agg.hspCount).toBe(2);
  });

  it("keeps an empty-sseqid bucket (frontend doesn't drop them; backend does)", () => {
    const hits: BlastHit[] = [
      baseHit,
      { ...baseHit, sseqid: "", bitscore: 100 },
    ];
    const map = buildSubjectAggregates(hits);
    expect(map.size).toBe(2);
    expect(map.get("")?.hspCount).toBe(1);
    expect(map.get("NC_001")?.hspCount).toBe(1);
  });
});
