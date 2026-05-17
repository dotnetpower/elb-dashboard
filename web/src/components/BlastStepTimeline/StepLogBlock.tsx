import { useState } from "react";
import { Check, Copy } from "lucide-react";

import type { StepState } from "./constants";

// Premium log block with summary + collapsible detail, line numbers,
// per-line styling, and a copy button.
export function StepLogBlock({
  log,
  state,
  stepKey,
}: {
  log: string;
  state: StepState;
  stepKey: string;
}) {
  const [copied, setCopied] = useState(false);
  const [isExpanded, setIsExpanded] = useState(false);

  // Split into summary (first line) and detail (rest with line numbers).
  const delimIdx = log.indexOf("---");
  const hasSections = delimIdx > 0;
  const allLines = log.split("\n");
  // Summary = text before the first "---" section, or first line if
  // multi-line without "---".
  const summary = hasSections
    ? log.slice(0, delimIdx).trim()
    : allLines.length <= 2
      ? log
      : allLines[0];
  // Detail = everything from "---" onward, or lines 2+ if no "---" but
  // multi-line.
  const detail = hasSections
    ? log.slice(delimIdx).trim()
    : allLines.length > 2
      ? allLines.slice(1).join("\n")
      : null;
  const detailLines = detail?.split("\n") ?? [];
  const isLong = detailLines.length > 40;

  const copyLog = () => {
    navigator.clipboard.writeText(log).catch(() => {});
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="step-log-block" data-state={state}>
      <div className="step-log-summary">
        <span>{summary}</span>
        <button className="step-log-copy" onClick={copyLog} title="Copy full log">
          {copied ? (
            <Check size={11} strokeWidth={2} />
          ) : (
            <Copy size={11} strokeWidth={1.5} />
          )}
          <span>{copied ? "Copied" : "Copy"}</span>
        </button>
      </div>

      {detail && (
        <div
          className={`step-log-detail${
            isLong && !isExpanded ? " step-log-detail--collapsed" : ""
          }`}
        >
          <div className="step-log-lines">
            {(isLong && !isExpanded ? detailLines.slice(0, 40) : detailLines).map(
              (line, i) => {
                let lineClass = "step-log-text";
                if (line.startsWith("WARNING") || line.startsWith("⚠"))
                  lineClass += " step-log-text--warn";
                else if (
                  line.startsWith("ERROR") ||
                  line.startsWith("✗") ||
                  /ErrorCode:|<Error>|ContainerNotFound|FATAL/.test(line)
                )
                  lineClass += " step-log-text--error";
                else if (
                  line.startsWith("✓") ||
                  line.includes("=ok") ||
                  line.includes("EXIT_CODE=0")
                )
                  lineClass += " step-log-text--ok";
                else if (line.startsWith("---"))
                  lineClass += " step-log-text--header";
                else if (line.startsWith("INFO:"))
                  lineClass += " step-log-text--info";
                return (
                  <div key={`${stepKey}-${i}`} className="step-log-line">
                    <span className="step-log-ln">{i + 1}</span>
                    <span className={lineClass}>{line || "\u00A0"}</span>
                  </div>
                );
              },
            )}
          </div>
          {isLong && !isExpanded && (
            <button className="step-log-expand" onClick={() => setIsExpanded(true)}>
              Show all {detailLines.length} lines
            </button>
          )}
          {isLong && isExpanded && (
            <button className="step-log-expand" onClick={() => setIsExpanded(false)}>
              Collapse
            </button>
          )}
        </div>
      )}
    </div>
  );
}
