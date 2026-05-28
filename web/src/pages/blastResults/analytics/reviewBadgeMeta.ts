/**
 * Review-badge tier metadata, mirrored from the backend classifier.
 *
 * Source of truth: `annotate_result_hit()` in
 * `api/services/blast/result_analytics.py`. If the backend thresholds
 * move, update this file in the same change — the snapshot test in
 * `reviewBadgeMeta.test.ts` will fail otherwise.
 */

import type { BlastHit } from "@/api/endpoints";

import { numberValue } from "./helpers";

export type ReviewStatus = NonNullable<BlastHit["review_status"]>;

export interface ReviewTier {
  key: ReviewStatus;
  /** Short label rendered inside the badge pill. */
  label: string;
  /** CSS variable token used for border + text color. */
  color: string;
  /** One-line human reason — matches the backend `review_reason`. */
  reason: string;
  /** Plain-English rule, shown on the threshold row. */
  rule: string;
  /** Per-field threshold expressions used to highlight the active row. */
  thresholds: {
    pident?: string;
    qcovs?: string;
    evalue?: string;
  };
}

export const REVIEW_TIERS: readonly ReviewTier[] = [
  {
    key: "strong_match",
    label: "Strong",
    color: "var(--success)",
    reason: "Near-exact, high-coverage HSP.",
    rule: "Strongest tier — treat as a confident identification.",
    thresholds: { pident: "≥ 99.5%", qcovs: "≥ 95%", evalue: "≤ 1e-20" },
  },
  {
    key: "review_priority",
    label: "Review",
    color: "var(--warning)",
    reason: "High-similarity HSP worth diagnostic review.",
    rule: "Very similar but not identical — worth a human look (near species, variant, or sequencing noise).",
    thresholds: { pident: "≥ 95%", qcovs: "≥ 80%", evalue: "≤ 1e-5" },
  },
  {
    key: "low_confidence",
    label: "Low",
    color: "var(--accent)",
    reason: "Moderate similarity or partial coverage.",
    rule: "Moderate signal — informational only.",
    thresholds: { pident: "≥ 90%", qcovs: "≥ 50%" },
  },
  {
    key: "weak_hit",
    label: "Weak",
    color: "var(--text-muted)",
    reason: "Low similarity or short coverage.",
    rule: "None of the higher tiers matched — usually background noise.",
    thresholds: {},
  },
  {
    key: "unclassified",
    label: "Unknown",
    color: "var(--text-muted)",
    reason: "Missing identity, e-value, or HSP query coverage.",
    rule: "One or more required fields are missing from the parsed result row.",
    thresholds: {},
  },
] as const;

export const REVIEW_TIER_BY_KEY: Record<ReviewStatus, ReviewTier> =
  REVIEW_TIERS.reduce(
    (acc, tier) => {
      acc[tier.key] = tier;
      return acc;
    },
    {} as Record<ReviewStatus, ReviewTier>,
  );

/**
 * List the required fields that are missing from a hit, so the popover
 * can tell the user *why* a row landed in `unclassified` instead of
 * just showing a generic "unknown" label.
 */
export function missingClassificationFields(hit: BlastHit): string[] {
  const missing: string[] = [];
  if (numberValue(hit.pident) === null) missing.push("% identity");
  if (numberValue(hit.evalue) === null) missing.push("E-value");
  if (numberValue(hit.qcovs) === null) missing.push("HSP query coverage");
  return missing;
}
