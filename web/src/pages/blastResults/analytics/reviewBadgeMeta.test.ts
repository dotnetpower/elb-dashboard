import { describe, expect, it } from "vitest";

import type { BlastHit } from "@/api/endpoints";

import {
  REVIEW_TIERS,
  REVIEW_TIER_BY_KEY,
  missingClassificationFields,
} from "./reviewBadgeMeta";

const baseHit: BlastHit = {
  qseqid: "queryA",
  sseqid: "NC_001",
  pident: 99.7,
  length: 150,
  mismatch: 0,
  gapopen: 0,
  qstart: 1,
  qend: 150,
  sstart: 100,
  send: 249,
  evalue: 1e-50,
  bitscore: 289,
  qcovs: 98.0,
};

describe("REVIEW_TIERS", () => {
  it("exposes all five backend-mirrored tiers in priority order", () => {
    expect(REVIEW_TIERS.map((t) => t.key)).toEqual([
      "strong_match",
      "review_priority",
      "low_confidence",
      "weak_hit",
      "unclassified",
    ]);
  });

  it("pins the exact thresholds that the backend classifier uses", () => {
    // Source of truth: annotate_result_hit() in
    // api/services/blast/result_analytics.py. If you bump these, the
    // backend numbers must move in the same change.
    const strong = REVIEW_TIER_BY_KEY.strong_match;
    expect(strong.thresholds.pident).toBe("≥ 99.5%");
    expect(strong.thresholds.qcovs).toBe("≥ 95%");
    expect(strong.thresholds.evalue).toBe("≤ 1e-20");

    const review = REVIEW_TIER_BY_KEY.review_priority;
    expect(review.thresholds.pident).toBe("≥ 95%");
    expect(review.thresholds.qcovs).toBe("≥ 80%");
    expect(review.thresholds.evalue).toBe("≤ 1e-5");

    const low = REVIEW_TIER_BY_KEY.low_confidence;
    expect(low.thresholds.pident).toBe("≥ 90%");
    expect(low.thresholds.qcovs).toBe("≥ 50%");
    expect(low.thresholds.evalue).toBeUndefined();
  });

  it("renders distinct labels per tier", () => {
    const labels = REVIEW_TIERS.map((t) => t.label);
    expect(new Set(labels).size).toBe(labels.length);
    expect(labels).toEqual(["Strong", "Review", "Low", "Weak", "Unknown"]);
  });
});

describe("missingClassificationFields", () => {
  it("returns empty when identity, e-value, and qcovs are all present", () => {
    expect(missingClassificationFields(baseHit)).toEqual([]);
  });

  it("flags only the fields that cannot be parsed as numbers", () => {
    const broken: BlastHit = {
      ...baseHit,
      pident: "nope",
      qcovs: undefined,
    };
    expect(missingClassificationFields(broken)).toEqual([
      "% identity",
      "HSP query coverage",
    ]);
  });

  it("treats missing e-value as a classification gap", () => {
    const broken: BlastHit = { ...baseHit, evalue: "n/a" };
    expect(missingClassificationFields(broken)).toEqual(["E-value"]);
  });
});
