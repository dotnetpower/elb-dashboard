/**
 * Pure formatting helpers for the per-hit alignment export actions
 * (Copy alignment / Copy FASTA / Download FASTA). Kept in a separate
 * module from `AlignmentViewer.tsx` so the formatters have no React /
 * DOM dependencies and can be unit-tested directly.
 *
 * Responsibility: turn a `BlastHit` row into the text the researcher
 * downloads or pastes — pairwise text, two-record FASTA, and a safe
 * download filename. Anything that touches the clipboard or the DOM
 * lives in the React component, not here.
 */

import type { BlastHit } from "@/api/endpoints";
import {
  formatDecimal,
  formatEvalue,
  formatPercent,
  formatRange,
  numberValue,
} from "./helpers";

const PAIRWISE_BLOCK_SIZE = 60;
const FASTA_WRAP_WIDTH = 70;

/**
 * Build a plain-text pairwise alignment in the rough shape NCBI ships
 * for researchers who paste alignments into manuscripts. Format:
 *
 *   Query  <qstart>  <60 chars>
 *                    |||| ::: ...
 *   Sbjct  <sstart>  <60 chars>
 *
 * Position counters advance by column index, NOT by sequence length,
 * so gap-padding stays aligned across the three lines (matches the
 * on-screen renderer).
 */
export function buildPairwiseAlignmentText(hit: BlastHit): string {
  const qseq = String(hit.qseq ?? "");
  const sseq = String(hit.sseq ?? "");
  const qstart = numberValue(hit.qstart) ?? 1;
  const sstart = numberValue(hit.sstart) ?? 1;

  const headerLines = [
    `Query  ${hit.qseqid ?? "(query)"}  ${formatRange(hit.qstart, hit.qend)} / ${hit.qlen ?? "?"}`,
    `Sbjct  ${hit.sseqid ?? "(subject)"}  ${formatRange(hit.sstart, hit.send)} / ${hit.slen ?? "?"}`,
    `Score  ${formatDecimal(hit.bitscore, 1)} bits   E=${formatEvalue(hit.evalue)}   Identity=${formatPercent(hit.pident)}`,
    "",
  ];

  const out: string[] = [...headerLines];
  for (let i = 0; i < qseq.length; i += PAIRWISE_BLOCK_SIZE) {
    const qBlock = qseq.slice(i, i + PAIRWISE_BLOCK_SIZE);
    const sBlock = sseq.slice(i, i + PAIRWISE_BLOCK_SIZE);
    let matchLine = "";
    for (let j = 0; j < qBlock.length; j++) {
      if (qBlock[j] === sBlock[j]) matchLine += "|";
      else if (qBlock[j] !== "-" && sBlock[j] !== "-") matchLine += ":";
      else matchLine += " ";
    }
    const qPos = qstart + i;
    const sPos = sstart + i;
    out.push(`Query  ${String(qPos).padStart(6, " ")}  ${qBlock}`);
    out.push(`                ${matchLine}`);
    out.push(`Sbjct  ${String(sPos).padStart(6, " ")}  ${sBlock}`);
    out.push("");
  }
  return out.join("\n");
}

/**
 * Build a two-record FASTA string (query + subject) for the aligned
 * region. Gap characters are stripped so the records contain the
 * actual sequence the researcher can re-use in downstream tools.
 */
export function buildAlignmentFasta(hit: BlastHit): string {
  const qid = hit.qseqid || "query";
  const sid = hit.sseqid || "subject";
  const stitle = hit.stitle ? ` ${hit.stitle}` : "";
  const qseq = String(hit.qseq ?? "").replace(/-/g, "");
  const sseq = String(hit.sseq ?? "").replace(/-/g, "");
  const qRange = `${hit.qstart ?? "?"}-${hit.qend ?? "?"}`;
  const sRange = `${hit.sstart ?? "?"}-${hit.send ?? "?"}`;
  return [
    `>${qid} aligned_region=${qRange}`,
    wrapFasta(qseq),
    `>${sid}${stitle} aligned_region=${sRange}`,
    wrapFasta(sseq),
    "",
  ].join("\n");
}

/**
 * Wrap a sequence at `width` columns (default 70, matching most NCBI
 * FASTA exports). Returns an empty string for empty input rather than
 * a trailing newline so the caller controls the surrounding format.
 */
export function wrapFasta(seq: string, width: number = FASTA_WRAP_WIDTH): string {
  if (!seq) return "";
  const lines: string[] = [];
  for (let i = 0; i < seq.length; i += width) {
    lines.push(seq.slice(i, i + width));
  }
  return lines.join("\n");
}

/**
 * Build a safe cross-OS download filename for the alignment FASTA.
 * Replaces every non-`[A-Za-z0-9._-]` character with `_` so Windows /
 * macOS / Linux all accept it, then joins the two identifiers with `__`.
 */
export function buildAlignmentExportFilename(hit: BlastHit): string {
  const q = (hit.qseqid || "query").replace(/[^A-Za-z0-9._-]/g, "_");
  const s = (hit.sseqid || "subject").replace(/[^A-Za-z0-9._-]/g, "_");
  return `${q}__${s}.fasta`;
}
