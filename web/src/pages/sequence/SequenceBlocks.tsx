/**
 * NCBI-style aligned + colored sequence renderer for the Sequence Detail page.
 *
 * Responsibility: Take the raw FASTA text of a single nuccore record and render
 * the full sequence the way NCBI's GenBank ORIGIN block does — a left position
 * gutter (1-based, right-aligned) followed by six space-separated groups of ten
 * residues per row (60/row) — and colorise each nucleotide (A/C/G/T/U) so the
 * composition is scannable. No truncation: the entire resolved sequence is
 * shown.
 * Edit boundaries: UI-only, pure render from the FASTA string prop. No network,
 * no NCBI calls (the text is already backend-proxied into `fasta`). Performance
 * caps live here (`COLOR_AUTO_LIMIT`, `COLOR_HARD_LIMIT`) — keep them so a
 * multi-megabyte genome record cannot freeze the tab by emitting millions of
 * per-base spans.
 * Key entry points: `SequenceBlocks` (default + named export).
 * Risky contracts: per-base coloring emits one <span> per residue, so it is
 * gated by length. Above `COLOR_AUTO_LIMIT` we render the full sequence as one
 * aligned <pre> (single DOM node, plain) and offer an explicit opt-in to
 * colorise up to `COLOR_HARD_LIMIT`; beyond that hard cap coloring stays off to
 * protect the browser. The aligned text itself is always complete regardless of
 * the color mode.
 * Validation: `cd web && npm run build` (type-check) + eyeball a small record
 * (colored rows) and a large one (plain aligned <pre>, opt-in colorize note).
 */
import { useMemo, useState, type CSSProperties, type ReactNode } from "react";

import { IUPAC_AMBIGUOUS } from "./sequenceAnalysis";

const ROW_WIDTH = 60;
const GROUP_WIDTH = 10;
// Colorize per-base automatically up to this length (≈ this many <span>s).
const COLOR_AUTO_LIMIT = 20_000;
// Absolute ceiling for opt-in colorization; above this we keep plain text so a
// genome-scale record can never emit millions of spans and hang the tab.
const COLOR_HARD_LIMIT = 200_000;

// Muted, glass-theme-friendly nucleotide palette readable on the dark code
// surface. Mirrors the common A=green / C=blue / G=amber / T=red convention.
const NT_COLORS: Record<string, string> = {
  A: "#6ee7a8",
  C: "#7cc4ff",
  G: "#f5c97b",
  T: "#f59e9e",
  U: "#f59e9e",
};

interface ParsedFasta {
  header: string | null;
  seq: string;
}

// Strip every defline (`>`-prefixed) and concatenate the residue lines into a
// single whitespace-free string. A single nuccore accession resolves to one
// record; if a multi-record FASTA ever arrives we still render the full
// concatenation rather than silently dropping data.
function parseFastaSequence(fasta: string): ParsedFasta {
  const lines = fasta.split(/\r?\n/);
  const headerLine = lines.find((line) => line.startsWith(">")) ?? null;
  const seq = lines
    .filter((line) => line.length > 0 && !line.startsWith(">"))
    .join("")
    .replace(/\s+/g, "");
  return {
    header: headerLine ? headerLine.slice(1).trim() : null,
    seq,
  };
}

// Heuristic: sample the head of the sequence; if <85% of residues are
// nucleotide letters treat it as a protein and skip per-base coloring (the
// 20-amino-acid palette is a separate concern out of scope here).
function isLikelyProtein(seq: string): boolean {
  if (!seq) return false;
  const sample = seq.slice(0, 200).toUpperCase();
  let nt = 0;
  for (const ch of sample) if ("ACGTUN".includes(ch)) nt += 1;
  return nt / sample.length < 0.85;
}

// Render the full sequence as aligned NCBI-ORIGIN text (no color), used for the
// single-<pre> fast path on large records and for the copy fallback.
function formatAlignedText(seq: string, posWidth: number): string {
  const out: string[] = [];
  for (let i = 0; i < seq.length; i += ROW_WIDTH) {
    const rowBases = seq.slice(i, i + ROW_WIDTH);
    const groups: string[] = [];
    for (let g = 0; g < rowBases.length; g += GROUP_WIDTH) {
      groups.push(rowBases.slice(g, g + GROUP_WIDTH));
    }
    const pos = String(i + 1).padStart(posWidth, " ");
    out.push(`${pos}  ${groups.join(" ")}`);
  }
  return out.join("\n");
}

const monoFamily = "var(--font-mono, monospace)";

export function SequenceBlocks({
  fasta,
  highlight,
}: {
  fasta: string;
  highlight?: { start: number; stop: number } | null;
}) {
  const { header, seq } = useMemo(() => parseFastaSequence(fasta), [fasta]);
  const protein = useMemo(() => isLikelyProtein(seq), [seq]);
  const posWidth = Math.max(String(seq.length).length, 4);

  // Above the auto limit we default to the plain (uncolored) aligned view;
  // `forceColor` lets a researcher opt into coloring up to the hard cap.
  const [forceColor, setForceColor] = useState(false);
  const colorEligible = !protein && seq.length > 0;
  const colorize =
    colorEligible &&
    (seq.length <= COLOR_AUTO_LIMIT ||
      (forceColor && seq.length <= COLOR_HARD_LIMIT));

  const hlStart = highlight?.start ?? -1;
  const hlStop = highlight?.stop ?? -1;

  const rows = useMemo(() => {
    if (!colorize) return null;
    const built: { pos: number; bases: string }[] = [];
    for (let i = 0; i < seq.length; i += ROW_WIDTH) {
      built.push({ pos: i + 1, bases: seq.slice(i, i + ROW_WIDTH) });
    }
    return built;
  }, [colorize, seq]);

  if (seq.length === 0) {
    return <p className="muted" style={{ margin: 0 }}>No sequence residues to display.</p>;
  }

  return (
    <div style={{ display: "grid", gap: 8 }}>
      {header && (
        <div
          style={{
            fontFamily: monoFamily,
            fontSize: 12,
            color: "var(--text-muted)",
            wordBreak: "break-word",
          }}
          title="FASTA definition line"
        >
          &gt;{header}
        </div>
      )}

      <div
        style={{
          display: "flex",
          gap: 12,
          alignItems: "center",
          flexWrap: "wrap",
          fontSize: 11,
          color: "var(--text-muted)",
        }}
      >
        <span>{seq.length.toLocaleString()} {protein ? "residues" : "bp"}</span>
        {colorize && (
          <span style={{ display: "inline-flex", gap: 10, alignItems: "center" }}>
            {(["A", "C", "G", protein ? "" : "T"] as const)
              .filter(Boolean)
              .map((base) => (
                <span key={base} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                  <span
                    style={{
                      width: 9,
                      height: 9,
                      borderRadius: 2,
                      background: NT_COLORS[base],
                      display: "inline-block",
                    }}
                  />
                  {base}
                </span>
              ))}
            <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              <span
                style={{
                  textDecoration: "underline dotted",
                  textUnderlineOffset: "2px",
                  color: "#f0a868",
                  fontWeight: 600,
                }}
              >
                N
              </span>
              N / ambiguous
            </span>
          </span>
        )}
        {colorEligible && !colorize && seq.length <= COLOR_HARD_LIMIT && (
          <button
            type="button"
            className="glass-button glass-button--ghost"
            style={{ fontSize: 11, padding: "2px 8px" }}
            onClick={() => setForceColor(true)}
            title="Colorizing a long sequence can be slow on modest hardware"
          >
            Colorize (slower)
          </button>
        )}
        {colorEligible && !colorize && seq.length > COLOR_HARD_LIMIT && (
          <span>Coloring disabled for sequences over {COLOR_HARD_LIMIT.toLocaleString()} bp.</span>
        )}
      </div>

      <div
        style={{
          maxHeight: "70vh",
          overflow: "auto",
          padding: "10px 12px",
          borderRadius: 8,
          background: "rgba(0,0,0,0.18)",
        }}
      >
        {colorize && rows ? (
          <div style={{ fontFamily: monoFamily, fontSize: 12, lineHeight: "18px" }}>
            {rows.map((row) => (
              <div
                key={row.pos}
                style={
                  {
                    display: "flex",
                    gap: 12,
                    // Let the browser skip painting off-screen rows so even a
                    // few thousand colored rows scroll smoothly.
                    contentVisibility: "auto",
                    containIntrinsicSize: "0 18px",
                  } as CSSProperties
                }
              >
                <span
                  style={{ color: "var(--text-muted)", userSelect: "none", textAlign: "right", minWidth: `${posWidth}ch` }}
                >
                  {row.pos}
                </span>
                <span style={{ whiteSpace: "pre" }}>
                  {renderColoredBases(row.bases, row.pos, hlStart, hlStop)}
                </span>
              </div>
            ))}
          </div>
        ) : (
          <pre
            style={{
              margin: 0,
              fontFamily: monoFamily,
              fontSize: 12,
              lineHeight: "18px",
              whiteSpace: "pre",
            }}
          >
            {formatAlignedText(seq, posWidth)}
          </pre>
        )}
      </div>
    </div>
  );
}

// Build the colored residues for one 60-base row, inserting a plain space after
// every 10-base group and applying the hit-range highlight background.
function renderColoredBases(
  bases: string,
  rowStart: number,
  hlStart: number,
  hlStop: number,
): ReactNode[] {
  const nodes: ReactNode[] = [];
  for (let i = 0; i < bases.length; i += 1) {
    if (i > 0 && i % GROUP_WIDTH === 0) nodes.push(" ");
    const ch = bases[i];
    const upper = ch.toUpperCase();
    const absPos = rowStart + i; // 1-based position of this residue
    const inHighlight = hlStart > 0 && absPos >= hlStart && absPos <= hlStop;
    const isN = upper === "N";
    const isAmbiguous = IUPAC_AMBIGUOUS.has(upper);
    const color = NT_COLORS[upper] ?? (isN || isAmbiguous ? "#f0a868" : "var(--text-muted)");
    const style: CSSProperties = { color };
    // Lowercase = soft-masked (repeats); dim it the way genome browsers do.
    if (ch !== upper) style.opacity = 0.6;
    // N / IUPAC-ambiguous residues are unusable for primer/probe design;
    // mark them so they are not silently read as a normal base.
    if (isN || isAmbiguous) {
      style.textDecoration = "underline dotted";
      style.textUnderlineOffset = "2px";
      style.fontWeight = 600;
    }
    if (inHighlight) {
      style.background = "rgba(245, 201, 123, 0.28)";
      style.borderRadius = "2px";
    }
    nodes.push(
      <span key={i} style={style}>
        {ch}
      </span>,
    );
  }
  return nodes;
}

export default SequenceBlocks;
