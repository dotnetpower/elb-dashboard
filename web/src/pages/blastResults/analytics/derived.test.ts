import { describe, expect, it } from "vitest";

import type { BlastHit, BlastJobSummary } from "@/api/endpoints";
import {
  buildMethodsText,
  buildSubjectTracks,
  buildTriagePoints,
  derepByRank,
  evalueConfidence,
  hitQueryCoverage,
  parityVerdict,
  searchSpacePin,
  triageQuadrantCounts,
} from "./derived";

function hit(overrides: Partial<BlastHit> = {}): BlastHit {
  return {
    qseqid: "Q1",
    sseqid: "NM_000546.6",
    pident: 99,
    length: 500,
    mismatch: 2,
    gapopen: 0,
    qstart: 1,
    qend: 500,
    sstart: 1,
    send: 500,
    evalue: 1e-120,
    bitscore: 900,
    qlen: 500,
    slen: 2500,
    ...overrides,
  };
}

describe("hitQueryCoverage", () => {
  it("prefers the qcovs column when present", () => {
    expect(hitQueryCoverage(hit({ qcovs: 73 }))).toBe(73);
  });

  it("derives coverage from query span when qcovs is absent", () => {
    expect(hitQueryCoverage(hit({ qstart: 1, qend: 250, qlen: 500, qcovs: undefined }))).toBe(50);
  });

  it("returns null when qlen is missing", () => {
    expect(hitQueryCoverage(hit({ qlen: undefined, qcovs: undefined }))).toBeNull();
  });
});

describe("buildTriagePoints", () => {
  it("classifies the four quadrants", () => {
    const points = buildTriagePoints([
      hit({ qcovs: 90, pident: 95 }), // ortholog
      hit({ qcovs: 90, pident: 40 }), // divergent
      hit({ qcovs: 10, pident: 95 }), // partial
      hit({ qcovs: 10, pident: 40 }), // marginal
    ]);
    expect(points.map((p) => p.quadrant)).toEqual([
      "ortholog",
      "divergent",
      "partial",
      "marginal",
    ]);
    expect(triageQuadrantCounts(points)).toEqual({
      ortholog: 1,
      divergent: 1,
      partial: 1,
      marginal: 1,
    });
  });

  it("drops hits with no usable coordinates", () => {
    const points = buildTriagePoints([
      hit({ qcovs: undefined, qlen: undefined, pident: 90 }),
    ]);
    expect(points).toHaveLength(0);
  });

  it("maps review_status into buckets", () => {
    const [p] = buildTriagePoints([hit({ qcovs: 90, review_status: "review_priority" })]);
    expect(p.bucket).toBe("review");
  });
});

describe("derepByRank", () => {
  const hits = [
    hit({ sseqid: "A", sscinames: "Escherichia coli", bitscore: 800, evalue: 1e-90 }),
    hit({ sseqid: "B", sscinames: "Escherichia coli", bitscore: 950, evalue: 1e-120 }),
    hit({ sseqid: "C", sscinames: "Escherichia fergusonii", bitscore: 600, evalue: 1e-60 }),
    hit({ sseqid: "D", sscinames: "Salmonella enterica", bitscore: 700, evalue: 1e-70 }),
  ];

  it("collapses by species keeping the best representative", () => {
    const rows = derepByRank(hits, "species");
    const coli = rows.find((r) => r.label === "Escherichia coli");
    expect(coli?.hitCount).toBe(2);
    expect(coli?.bestHit.sseqid).toBe("B");
    expect(coli?.bestBitscore).toBe(950);
  });

  it("collapses by genus", () => {
    const rows = derepByRank(hits, "genus");
    const escherichia = rows.find((r) => r.label === "Escherichia");
    expect(escherichia?.hitCount).toBe(3);
  });

  it("sorts rows by best bitscore desc", () => {
    const rows = derepByRank(hits, "species");
    expect(rows[0].label).toBe("Escherichia coli");
  });

  it("buckets organism-less hits under Unassigned", () => {
    const rows = derepByRank([hit({ sscinames: undefined, stitle: undefined })], "species");
    expect(rows[0].label).toBe("Unassigned");
  });
});

describe("buildSubjectTracks", () => {
  it("flags strand flips", () => {
    const tracks = buildSubjectTracks([
      hit({ sseqid: "S1", sstart: 1, send: 100, qstart: 1 }),
      hit({ sseqid: "S1", sstart: 400, send: 300, qstart: 200 }),
    ]);
    expect(tracks[0].hspCount).toBe(2);
    expect(tracks[0].hasStrandFlip).toBe(true);
  });

  it("flags order inversion when subject coords disagree with query order", () => {
    const tracks = buildSubjectTracks([
      hit({ sseqid: "S2", sstart: 500, send: 600, qstart: 1 }),
      hit({ sseqid: "S2", sstart: 100, send: 200, qstart: 200 }),
    ]);
    expect(tracks[0].hasOrderInversion).toBe(true);
  });

  it("does not flag a single HSP", () => {
    const tracks = buildSubjectTracks([hit({ sseqid: "S3" })]);
    expect(tracks[0].hasOrderInversion).toBe(false);
    expect(tracks[0].hasStrandFlip).toBe(false);
  });
});

describe("evalueConfidence", () => {
  it("calls a tiny E-value essentially certain", () => {
    expect(evalueConfidence(1e-120).level).toBe("high");
  });

  it("calls a near-1 E-value marginal", () => {
    expect(evalueConfidence(0.4).level).toBe("low");
  });

  it("calls E-value >= 1 likely by chance", () => {
    expect(evalueConfidence(5).level).toBe("none");
  });

  it("handles missing values", () => {
    expect(evalueConfidence(undefined).level).toBe("none");
  });
});

describe("searchSpacePin", () => {
  it("reads the compatibility contract searchsp first", () => {
    const job = {
      provenance: {
        compatibility: { searchsp: 3.2e13, search_space_source: "pinned" },
      },
    } as unknown as BlastJobSummary;
    const pin = searchSpacePin(job);
    expect(pin.searchSpace).toBe(3.2e13);
    expect(pin.text).toContain("3.20e+13");
  });

  it("falls back to the submitted option", () => {
    const job = {
      payload: { db_effective_search_space: 1.5e10 },
    } as unknown as BlastJobSummary;
    expect(searchSpacePin(job).searchSpace).toBe(1.5e10);
  });

  it("reports when unpinned", () => {
    expect(searchSpacePin(null).searchSpace).toBeNull();
  });
});

describe("parityVerdict", () => {
  it("reports NCBI-equivalent for precise eligible runs", () => {
    const job = {
      provenance: { compatibility: { mode: "precise", eligible: true, warnings: [] } },
    } as unknown as BlastJobSummary;
    expect(parityVerdict(job).state).toBe("equivalent");
  });

  it("reports drift for calibration_required", () => {
    const job = {
      provenance: { compatibility: { mode: "calibration_required", eligible: false } },
    } as unknown as BlastJobSummary;
    expect(parityVerdict(job).state).toBe("drift");
  });

  it("reports unknown without a contract", () => {
    expect(parityVerdict(null).state).toBe("unknown");
  });
});

describe("buildMethodsText", () => {
  it("assembles a methods sentence from provenance", () => {
    const job = {
      program: "blastn",
      db: "core_nt",
      provenance: {
        blast: { program: "blastn", version: "2.17.0" },
        database: {
          name: "core_nt",
          snapshot: "2026-05-09",
          number_of_sequences: 125619662,
          number_of_letters: 1.04e12,
        },
        options: { evalue: 0.05 },
        compatibility: { mode: "precise", eligible: true, warnings: [], searchsp: 3.2e13 },
      },
      payload: {},
    } as unknown as BlastJobSummary;
    const text = buildMethodsText(job);
    expect(text).toContain("BLAST+ 2.17.0 blastn");
    expect(text).toContain("core_nt");
    expect(text).toContain("snapshot 2026-05-09");
    expect(text).toContain("E-value threshold of 0.05");
    expect(text).toContain("equivalent to a single full-database NCBI BLAST run");
  });

  it("degrades gracefully with an empty bundle", () => {
    const text = buildMethodsText({} as BlastJobSummary);
    expect(text).toContain("BLAST");
  });
});
