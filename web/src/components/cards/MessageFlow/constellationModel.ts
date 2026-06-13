/**
 * constellationModel — pure data-mapping helpers for the Message Flow
 * "Bounded Lanes (A1)" constellation.
 *
 * Responsibility: Translate raw `MessageFlowSnapshot` fields into the small
 *   derived values the D3 renderer needs (producer kind, job radius, link age
 *   style, parsed timestamps, deterministic spread offsets, hover tooltip).
 *   Pure functions only — no React, no D3, no DOM — so the mapping can be unit
 *   tested in the node vitest environment.
 * Edit boundaries: Keep these honest to the live contract. Missing inputs MUST
 *   degrade (minimum radius, neutral link age) rather than fabricate a value —
 *   there is no synthetic data here.
 * Key entry points: `producerKind`, `jobRadius`, `ageStyle`, `bornMs`,
 *   `spread01`, `jobTooltip`.
 * Risky contracts: `producerKind` returns "user" ONLY when an interactive
 *   `dashboard` submission contributed; every other source (external_api /
 *   servicebus / unknown) is "api". `ageStyle` thresholds (10s / 30s) are the
 *   shared visual contract for "recent vs aging" links.
 * Validation: `npx vitest run src/components/cards/MessageFlow/constellationModel.test.ts`.
 */
import type { MessageFlowBox } from "@/api/messageFlow";

import { querySizeLabel } from "./layout";

export interface LinkAgeStyle {
  /** Stroke width in px. */
  w: number;
  /** Stroke opacity 0..1. */
  op: number;
}

/**
 * A submitter is "user" only when an interactive dashboard submission
 * contributed; otherwise it is api/servicebus (the api-dominant case).
 */
export function producerKind(sources: string[] | undefined): "api" | "user" {
  return (sources ?? []).includes("dashboard") ? "user" : "api";
}

/** Job circle radius from the query letter count; minimum when unknown so we
 *  never fabricate a size. Capped so a pathologically large query cannot blow
 *  the node up to fill the whole broker region. */
export function jobRadius(querySize: number | null | undefined): number {
  if (querySize == null || querySize <= 0) return 4;
  return Math.min(18, 3.5 + Math.sqrt(querySize) / 9);
}

/** Parse an ISO/string timestamp to epoch ms, or null when unknown/invalid. */
export function bornMs(createdAt: string | null | undefined): number | null {
  if (!createdAt) return null;
  const t = Date.parse(createdAt);
  return Number.isFinite(t) ? t : null;
}

/**
 * Message age (seconds) → static link weight + opacity. No animation: recent
 * links read brighter/thicker, older links thinner/fainter. `null` born
 * (missing created_at) maps to a neutral middle style.
 */
export function ageStyle(born: number | null, now: number): LinkAgeStyle {
  if (born == null) return { w: 1.1, op: 0.22 };
  const age = (now - born) / 1000;
  if (age < 10) return { w: 1.8, op: 0.42 };
  if (age < 30) return { w: 1.2, op: 0.24 };
  return { w: 0.8, op: 0.12 };
}

/**
 * Deterministic 0..1-centred offset (-0.5..0.5) from a string id, used to
 * spread a status group into a calm band instead of piling every job onto one
 * focus point. Stable across re-renders for the same id.
 */
export function spread01(id: string): number {
  let h = 0;
  for (let i = 0; i < id.length; i += 1) h = (h * 31 + id.charCodeAt(i)) | 0;
  return (Math.abs(h) % 1000) / 1000 - 0.5;
}

/** Multi-line native tooltip text for a broker job circle. */
export function jobTooltip(b: MessageFlowBox): string {
  const lines = [
    `${b.program ?? "blast"} · ${querySizeLabel(b.query_size)}`,
    `status: ${b.status}${b.phase ? ` (${b.phase})` : ""}`,
  ];
  if (b.db) lines.push(`db: ${b.db}`);
  lines.push(`submitter: ${b.alias}`);
  lines.push(`cluster: ${b.cluster_name || "unassigned"}`);
  lines.push("click to view job JSON");
  return lines.join("\n");
}
