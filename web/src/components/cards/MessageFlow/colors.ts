/**
 * colors — stable, calm color assignment for message-flow submitter aliases.
 *
 * The same submitter alias always maps to the same palette entry (a simple
 * deterministic string hash), so a producer's colour matches the colour band
 * on its broker boxes. The palette stays inside the glassmorphic family:
 * low-saturation, sub-20% alpha fills against the deep-navy surface.
 */

export interface AliasTone {
  /** Solid-ish accent used for dots and box bands. */
  accent: string;
  /** Translucent fill for box backgrounds. */
  fill: string;
  /** Border tint. */
  border: string;
}

const PALETTE: AliasTone[] = [
  { accent: "rgba(110, 159, 255, 0.85)", fill: "rgba(110, 159, 255, 0.16)", border: "rgba(110, 159, 255, 0.32)" },
  { accent: "rgba(126, 200, 167, 0.85)", fill: "rgba(126, 200, 167, 0.16)", border: "rgba(126, 200, 167, 0.32)" },
  { accent: "rgba(201, 162, 232, 0.85)", fill: "rgba(201, 162, 232, 0.16)", border: "rgba(201, 162, 232, 0.32)" },
  { accent: "rgba(242, 178, 116, 0.85)", fill: "rgba(242, 178, 116, 0.16)", border: "rgba(242, 178, 116, 0.32)" },
  { accent: "rgba(127, 196, 222, 0.85)", fill: "rgba(127, 196, 222, 0.16)", border: "rgba(127, 196, 222, 0.32)" },
  { accent: "rgba(224, 156, 178, 0.85)", fill: "rgba(224, 156, 178, 0.16)", border: "rgba(224, 156, 178, 0.32)" },
  { accent: "rgba(176, 196, 138, 0.85)", fill: "rgba(176, 196, 138, 0.16)", border: "rgba(176, 196, 138, 0.32)" },
  { accent: "rgba(157, 165, 220, 0.85)", fill: "rgba(157, 165, 220, 0.16)", border: "rgba(157, 165, 220, 0.32)" },
];

function hashAlias(alias: string): number {
  let hash = 0;
  for (let i = 0; i < alias.length; i += 1) {
    hash = (hash * 31 + alias.charCodeAt(i)) | 0;
  }
  return Math.abs(hash);
}

export function aliasTone(alias: string): AliasTone {
  if (!alias) return PALETTE[0];
  return PALETTE[hashAlias(alias) % PALETTE.length];
}
