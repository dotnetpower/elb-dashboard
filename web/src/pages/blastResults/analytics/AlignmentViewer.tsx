import {
  formatDecimal,
  formatEvalue,
  formatInteger,
  formatPercent,
  formatRange,
  clampPercent,
  coverageOffset,
  coverageWidth,
  identityColor,
  numberValue,
  strandLabel,
} from "./helpers";
import {
  buildAlignmentExportFilename,
  buildAlignmentFasta,
  buildPairwiseAlignmentText,
} from "./alignmentExport";
import type { BlastHit } from "@/api/endpoints";
import { useState } from "react";

// Reused by the pairwise renderer. NCBI's pairwise view colors bases by
// chemistry; we keep our existing palette so existing screenshots/docs
// still apply.
const BASE_COLORS: Record<string, string> = {
  A: "#6ad6a3",
  T: "#e07b8a",
  G: "#f0c674",
  C: "#7aa7ff",
  U: "#e07b8a",
  R: "#7aa7ff",
  K: "#7aa7ff",
  H: "#7aa7ff",
  D: "#e07b8a",
  E: "#e07b8a",
  S: "#6ad6a3",
  N: "#6ad6a3",
  Q: "#6ad6a3",
  W: "#f0c674",
  F: "#f0c674",
  Y: "#f0c674",
  "-": "#555",
  "*": "#e07b8a",
};

export interface AlignmentViewerProps {
  hit: BlastHit;
}

/**
 * Per-hit pairwise alignment card. Headline mirrors NCBI's Alignments
 * stat line (Score / Expect / Identities `N/total (pct)` / Gaps `N/total
 * (pct)` / Strand) so researchers see the absolute numbers, not just the
 * percent.
 */
export function AlignmentViewer({ hit }: AlignmentViewerProps) {
  const qStart = numberValue(hit.qstart);
  const qEnd = numberValue(hit.qend);
  const sStart = numberValue(hit.sstart);
  const sEnd = numberValue(hit.send);
  const qLen = numberValue(hit.qlen);
  const sLen = numberValue(hit.slen);
  const alignmentLength = numberValue(hit.length);
  const mismatch = numberValue(hit.mismatch);
  const gaps = numberValue(hit.gaps ?? hit.gapopen) ?? 0;
  const identityPct = numberValue(hit.pident);

  // Identities count = length - mismatch - gaps (when we have all three).
  // NCBI prints `462/462(100%)`, so compute the fraction from canonical
  // columns and fall back gracefully when shards omitted one of them.
  const identityCount =
    alignmentLength !== null && mismatch !== null
      ? Math.max(0, alignmentLength - mismatch - gaps)
      : null;

  return (
    <div className="glass-card" style={{ padding: 16, marginBottom: 12, fontSize: 13 }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
        <div>
          <span style={{ fontWeight: 600 }}>Query:</span>{" "}
          <code className="code-val">{hit.qseqid}</code>
          <span className="muted" style={{ marginLeft: 12 }}>
            {formatRange(hit.qstart, hit.qend)}
            {qLen ? ` / ${qLen}` : ""}
          </span>
        </div>
        <div
          style={{
            display: "flex",
            gap: 12,
            alignItems: "center",
            flexWrap: "wrap",
            justifyContent: "flex-end",
          }}
        >
          <span style={{ color: identityColor(hit.pident) }}>
            {identityCount !== null && alignmentLength
              ? `${identityCount}/${alignmentLength} (${formatPercent(hit.pident)})`
              : formatPercent(hit.pident)}{" "}
            identity
          </span>
          <span className="muted">
            Gaps {gaps}/{alignmentLength ?? "—"} (
            {alignmentLength && alignmentLength > 0
              ? `${((gaps / alignmentLength) * 100).toFixed(1)}%`
              : "—"}
            )
          </span>
          <span className="muted">E={formatEvalue(hit.evalue)}</span>
          <span className="muted">{formatDecimal(hit.bitscore, 1)} bits</span>
          <span className="muted">Strand: {strandLabel(hit.sstart, hit.send)}</span>
        </div>
      </div>

      <div style={{ marginBottom: 10 }}>
        <span style={{ fontWeight: 600 }}>Subject:</span>{" "}
        <code className="code-val">{hit.sseqid}</code>
        {hit.stitle && (
          <span className="muted" style={{ marginLeft: 8 }}>
            {hit.stitle}
          </span>
        )}
        <span className="muted" style={{ marginLeft: 12 }}>
          {formatRange(hit.sstart, hit.send)}
          {sLen ? ` / ${sLen}` : ""}
        </span>
      </div>

      <div style={{ margin: "8px 0" }}>
        <CoverageBar
          label="Query"
          start={qStart}
          end={qEnd}
          total={qLen}
          fallbackPct={identityPct ?? 0}
          color={identityColor(hit.pident)}
          opacity={0.8}
        />
        <CoverageBar
          label="Sbjct"
          start={sStart}
          end={sEnd}
          total={sLen}
          fallbackPct={identityPct ?? 0}
          color={identityColor(hit.pident)}
          opacity={0.6}
        />
      </div>

      {hit.qseq && hit.sseq && qStart !== null && sStart !== null && (
        <div style={{ marginTop: 12, overflowX: "auto" }}>
          <SequenceAlignment
            qseq={hit.qseq}
            sseq={hit.sseq}
            qstart={qStart}
            sstart={sStart}
          />
        </div>
      )}

      <div
        style={{
          display: "flex",
          gap: 20,
          marginTop: 10,
          fontSize: 12,
          color: "var(--text-muted)",
          flexWrap: "wrap",
        }}
      >
        <span>Length: {formatInteger(hit.length)}</span>
        <span>Mismatches: {formatInteger(hit.mismatch)}</span>
        <span>Gaps: {formatInteger(hit.gaps ?? hit.gapopen)}</span>
        {hit.ppos !== undefined && <span>Positives: {formatPercent(hit.ppos)}</span>}
        {hit.qframe !== undefined && <span>Query frame: {hit.qframe}</span>}
        {hit.sframe !== undefined && <span>Subject frame: {hit.sframe}</span>}
      </div>

      <AlignmentExportActions hit={hit} />
    </div>
  );
}

/**
 * NCBI Web BLAST has "Download" + "GenPept/GenBank" + "Graphics" buttons
 * on each alignment card. Our equivalents are intentionally smaller in
 * scope: clipboard copy of the pairwise text, and a two-record FASTA
 * download (query + subject for the aligned region). Both run entirely
 * client-side from data already on the page — no extra backend round-trip.
 *
 * Buttons stay hidden when the alignment payload lacks ``qseq``/``sseq``
 * (e.g. a tabular-only result file) so we never surface a broken action.
 */
function AlignmentExportActions({ hit }: { hit: BlastHit }) {
  const [feedback, setFeedback] = useState<string | null>(null);
  const hasSequences = Boolean(hit.qseq && hit.sseq);
  if (!hasSequences) return null;

  const showFeedback = (message: string) => {
    setFeedback(message);
    window.setTimeout(() => setFeedback(null), 2000);
  };

  const handleCopyAlignment = async () => {
    try {
      await navigator.clipboard.writeText(buildPairwiseAlignmentText(hit));
      showFeedback("Alignment copied");
    } catch {
      showFeedback("Copy failed");
    }
  };

  const handleCopyFasta = async () => {
    try {
      await navigator.clipboard.writeText(buildAlignmentFasta(hit));
      showFeedback("FASTA copied");
    } catch {
      showFeedback("Copy failed");
    }
  };

  const handleDownloadFasta = () => {
    const text = buildAlignmentFasta(hit);
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = buildAlignmentExportFilename(hit);
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    URL.revokeObjectURL(url);
    showFeedback("FASTA downloaded");
  };

  return (
    <div
      style={{
        display: "flex",
        gap: 8,
        marginTop: 10,
        flexWrap: "wrap",
        alignItems: "center",
      }}
    >
      <button type="button" className="btn-tertiary" onClick={handleCopyAlignment}>
        Copy alignment
      </button>
      <button type="button" className="btn-tertiary" onClick={handleCopyFasta}>
        Copy FASTA
      </button>
      <button type="button" className="btn-tertiary" onClick={handleDownloadFasta}>
        Download FASTA
      </button>
      {feedback && (
        <span className="muted" style={{ fontSize: 12 }}>
          {feedback}
        </span>
      )}
    </div>
  );
}

/**
 * Build a plain-text pairwise alignment, two-record FASTA, and the safe
 * download filename — all in `./alignmentExport`. Keep this file focused
 * on rendering; export formatters belong with their own tests.
 */

function CoverageBar({
  label,
  start,
  end,
  total,
  fallbackPct,
  color,
  opacity,
}: {
  label: string;
  start: number | null;
  end: number | null;
  total: number | null;
  fallbackPct: number;
  color: string;
  opacity: number;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        marginBottom: 4,
      }}
    >
      <span className="muted" style={{ minWidth: 40, fontSize: 11 }}>
        {label}
      </span>
      <div
        style={{
          position: "relative",
          flex: 1,
          height: 16,
          background: "var(--glass-bg)",
          borderRadius: 3,
        }}
      >
        {start !== null && end !== null && total ? (
          <div
            style={{
              position: "absolute",
              left: `${coverageOffset(start, end, total)}%`,
              width: `${coverageWidth(start, end, total)}%`,
              height: "100%",
              borderRadius: 3,
              background: color,
              opacity,
            }}
          />
        ) : (
          <div
            style={{
              width: `${clampPercent(fallbackPct)}%`,
              height: "100%",
              borderRadius: 3,
              background: "var(--accent)",
              opacity,
            }}
          />
        )}
      </div>
    </div>
  );
}

function SequenceAlignment({
  qseq,
  sseq,
  qstart,
  sstart,
}: {
  qseq: string;
  sseq: string;
  qstart: number;
  sstart: number;
}) {
  const blockSize = 60;
  const blocks: Array<{ q: string; m: string; s: string; qpos: number; spos: number }> =
    [];
  for (let i = 0; i < qseq.length; i += blockSize) {
    const qBlock = qseq.slice(i, i + blockSize);
    const sBlock = sseq.slice(i, i + blockSize);
    let matchLine = "";
    for (let j = 0; j < qBlock.length; j++) {
      if (qBlock[j] === sBlock[j]) matchLine += "|";
      else if (qBlock[j] !== "-" && sBlock[j] !== "-") matchLine += ":";
      else matchLine += " ";
    }
    blocks.push({
      q: qBlock,
      m: matchLine,
      s: sBlock,
      qpos: qstart + i,
      spos: sstart + i,
    });
  }

  return (
    <div
      style={{
        fontFamily: "var(--font-mono, monospace)",
        fontSize: 12,
        lineHeight: 1.6,
      }}
    >
      {blocks.map((block, index) => (
        <div key={`${block.qpos}-${block.spos}-${index}`} style={{ marginBottom: 8 }}>
          <div style={{ display: "flex" }}>
            <span
              className="muted"
              style={{ minWidth: 60, textAlign: "right", marginRight: 8 }}
            >
              Q {block.qpos}
            </span>
            <span>
              {block.q.split("").map((char, charIndex) => (
                <span
                  key={`${index}-q-${charIndex}`}
                  style={{
                    color: BASE_COLORS[char.toUpperCase()] ?? "var(--text-primary)",
                  }}
                >
                  {char}
                </span>
              ))}
            </span>
          </div>
          <div style={{ display: "flex" }}>
            <span style={{ minWidth: 60, marginRight: 8 }} />
            <span style={{ color: "var(--text-muted)" }}>{block.m}</span>
          </div>
          <div style={{ display: "flex" }}>
            <span
              className="muted"
              style={{ minWidth: 60, textAlign: "right", marginRight: 8 }}
            >
              S {block.spos}
            </span>
            <span>
              {block.s.split("").map((char, charIndex) => (
                <span
                  key={`${index}-s-${charIndex}`}
                  style={{
                    color: BASE_COLORS[char.toUpperCase()] ?? "var(--text-primary)",
                  }}
                >
                  {char}
                </span>
              ))}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}
