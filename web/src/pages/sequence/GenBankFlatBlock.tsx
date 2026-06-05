/**
 * Structured, accessible renderer for the GenBank flat-file header block.
 *
 * Responsibility: Replace the single undifferentiated `<pre>` dump on the
 * Sequence Detail page with a line-structured view that colourises the leading
 * GenBank tag column (LOCUS / DEFINITION / ACCESSION / …), offers a soft-wrap
 * toggle so long DEFINITION / COMMENT lines do not force horizontal scrolling,
 * and exposes a copy button — matching the FASTA block's affordances.
 * Edit boundaries: Presentational only. Input is the pre-formatted line array
 * from `genbankFlatLines` (fixed 12-char tag column); no parsing of the
 * underlying record here.
 * Key entry points: `GenBankFlatBlock`.
 * Risky contracts: relies on the 12-character tag column produced by
 * `genbankFlatLines` in SequenceDetail.tsx.
 * Validation: `cd web && npm run build` + eyeball the GenBank record card.
 */
import { useState } from "react";
import { Check, Copy, WrapText } from "lucide-react";

import { useTransientState } from "../../hooks/useTransientState";

const TAG_WIDTH = 12;

interface FlatLine {
  tag: string | null;
  body: string;
}

function splitFlatLine(line: string): FlatLine {
  const head = line.slice(0, TAG_WIDTH);
  const body = line.slice(TAG_WIDTH);
  // A continuation line is all-spaces in the tag column; ORGANISM is indented
  // two spaces but still carries a tag word.
  const trimmed = head.trim();
  if (trimmed.length === 0) {
    return { tag: null, body: line.trimStart() };
  }
  return { tag: trimmed, body };
}

export function GenBankFlatBlock({
  lines,
  rawText,
}: {
  lines: string[];
  rawText: string;
}) {
  const [wrap, setWrap] = useState(false);
  const [copied, flashCopied] = useTransientState(false);

  const copy = () => {
    if (!navigator.clipboard?.writeText) return;
    void navigator.clipboard.writeText(rawText).then(() => flashCopied(true, 1500));
  };

  return (
    <div className="genbank-flat">
      <div className="genbank-flat__tools">
        <button
          type="button"
          className="glass-button glass-button--ghost genbank-flat__btn"
          aria-pressed={wrap}
          onClick={() => setWrap((w) => !w)}
          title={wrap ? "Disable line wrapping" : "Wrap long lines"}
        >
          <WrapText size={12} strokeWidth={1.5} />
          {wrap ? "No wrap" : "Wrap"}
        </button>
        <button
          type="button"
          className="glass-button glass-button--ghost genbank-flat__btn"
          onClick={copy}
          title="Copy the GenBank header block"
        >
          {copied ? <Check size={12} strokeWidth={1.5} /> : <Copy size={12} strokeWidth={1.5} />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <ol
        className={`genbank-flat__lines${wrap ? " genbank-flat__lines--wrap" : ""}`}
        aria-label="GenBank flat-file header"
      >
        {lines.map((line, i) => {
          const { tag, body } = splitFlatLine(line);
          return (
            <li className="genbank-flat__line" key={`gb-${i}`}>
              {tag ? (
                <span className="genbank-flat__tag">{tag}</span>
              ) : (
                <span className="genbank-flat__tag genbank-flat__tag--cont" aria-hidden="true" />
              )}
              <span className="genbank-flat__body">{body || "\u00A0"}</span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
