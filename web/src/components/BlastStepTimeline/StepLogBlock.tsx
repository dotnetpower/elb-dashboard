import { useTransientState } from "../../hooks/useTransientState";
import { Check, Copy } from "lucide-react";

import type { StepState } from "./constants";

const ANSI_PATTERN = /\x1B\[[0-9;?]*[ -/]*[@-~]/g;
const TIMESTAMP_PATTERN =
  /^(\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\]?)\s*/;

type LineToken = { kind: "ts" | "text"; value: string; className?: string };

function stripAnsi(value: string): string {
  return value.replace(ANSI_PATTERN, "");
}

function classifyLineKind(raw: string): string {
  const line = raw.trimStart();
  if (!line) return "step-log-text";
  if (
    line.startsWith("ERROR") ||
    line.startsWith("FATAL") ||
    line.startsWith("✗") ||
    /\b(Traceback|panic:|Exception:|ContainerNotFound)\b/.test(line) ||
    /ErrorCode:|<Error>/.test(line)
  ) {
    return "step-log-text step-log-text--error";
  }
  if (
    line.startsWith("WARNING") ||
    line.startsWith("WARN") ||
    line.startsWith("⚠") ||
    /\b(deprecated|skipped)\b/i.test(line)
  ) {
    return "step-log-text step-log-text--warn";
  }
  if (
    line.startsWith("✓") ||
    line.includes("EXIT_CODE=0") ||
    /\b(SUCCESS|Completed|completed successfully)\b/.test(line)
  ) {
    return "step-log-text step-log-text--ok";
  }
  if (line.startsWith("---") || line.startsWith("===") || line.startsWith("###")) {
    return "step-log-text step-log-text--header";
  }
  if (line.startsWith("$ ") || line.startsWith("> ") || line.startsWith("# ")) {
    return "step-log-text step-log-text--cmd";
  }
  if (/^(INFO|DEBUG|TRACE|NOTE)[: ]/i.test(line)) {
    return "step-log-text step-log-text--info";
  }
  if (
    /\b(BLAST RUNTIME|RUN END|Database:|Posted date:|Database size:|Number of sequences|Number of letters)\b/.test(
      line,
    ) ||
    /^(elastic-blast|kubectl|az |azcopy|blastn|blastp|blastx|tblastn|tblastx)\b/.test(line)
  ) {
    return "step-log-text step-log-text--blast";
  }
  return "step-log-text";
}

function tokeniseLine(raw: string): LineToken[] {
  const cleaned = stripAnsi(raw);
  const tokens: LineToken[] = [];
  let body = cleaned;
  const match = body.match(TIMESTAMP_PATTERN);
  if (match) {
    tokens.push({ kind: "ts", value: match[1] });
    body = body.slice(match[0].length);
  }
  tokens.push({ kind: "text", value: body, className: classifyLineKind(body) });
  return tokens;
}

// Premium log block with summary + full detail (no scrollbar, no fold),
// per-line CI-style syntax highlighting, line numbers, and a copy button.
export function StepLogBlock({
  log,
  state,
  stepKey,
}: {
  log: string;
  state: StepState;
  stepKey: string;
}) {
  const [copied, flashCopied] = useTransientState(false);

  const delimIdx = log.indexOf("---");
  const hasSections = delimIdx > 0;
  const allLines = log.split("\n");
  const summary = hasSections
    ? log.slice(0, delimIdx).trim()
    : allLines.length <= 2
      ? log
      : allLines[0];
  const detail = hasSections
    ? log.slice(delimIdx).trim()
    : allLines.length > 2
      ? allLines.slice(1).join("\n")
      : null;
  const detailLines = detail?.split("\n") ?? [];

  const copyLog = () => {
    navigator.clipboard.writeText(log).catch(() => {});
    flashCopied(true);
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
        <div className="step-log-detail">
          <div className="step-log-lines">
            {detailLines.map((line, i) => {
              const tokens = tokeniseLine(line);
              return (
                <div key={`${stepKey}-${i}`} className="step-log-line">
                  <span className="step-log-ln">{i + 1}</span>
                  <span className="step-log-row">
                    {tokens.map((tok, j) =>
                      tok.kind === "ts" ? (
                        <span
                          key={`${stepKey}-${i}-ts-${j}`}
                          className="step-log-ts"
                        >
                          {tok.value}
                        </span>
                      ) : (
                        <span
                          key={`${stepKey}-${i}-tx-${j}`}
                          className={tok.className}
                        >
                          {tok.value || "\u00A0"}
                        </span>
                      ),
                    )}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
