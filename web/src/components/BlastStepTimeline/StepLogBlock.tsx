import { useMemo, useState } from "react";
import {
  AlertTriangle,
  Check,
  Clock,
  Copy,
  Download,
  Minus,
  Plus,
  Search,
  WrapText,
  XCircle,
} from "lucide-react";

import { useTransientState } from "../../hooks/useTransientState";
import type { StepState } from "./constants";
import {
  countIssues,
  parseStepLog,
  shortenTimestamp,
  splitSummaryDetail,
  type ParsedLogLine,
} from "./parseStepLog";

type LogFilter = "all" | "issues";

// Render groups for the detail body: a single visible line, or a folded run
// of low-level credential / HTTP noise lines.
type RenderGroup =
  | { kind: "line"; line: ParsedLogLine }
  | { kind: "noise"; lines: ParsedLogLine[] };

const MIN_NOISE_RUN = 3;

// Reader font-size steps (px) the user can cycle through with the A-/A+
// controls. Index 1 is the default; clamped to the array bounds.
const FONT_SIZE_STEPS_PX = [10.5, 11.5, 12.5, 14] as const;
const DEFAULT_FONT_STEP = 1;

const SEVERITY_CLASS: Record<ParsedLogLine["severity"], string> = {
  error: "step-log-text step-log-text--error",
  warn: "step-log-text step-log-text--warn",
  ok: "step-log-text step-log-text--ok",
  info: "step-log-text step-log-text--info",
  header: "step-log-text step-log-text--header",
  cmd: "step-log-text step-log-text--cmd",
  blast: "step-log-text step-log-text--blast",
  text: "step-log-text",
};

/** Group consecutive noise lines so long credential/HTTP runs collapse. */
function groupDetail(lines: ParsedLogLine[]): RenderGroup[] {
  const groups: RenderGroup[] = [];
  let run: ParsedLogLine[] = [];
  const flush = () => {
    if (run.length === 0) return;
    if (run.length >= MIN_NOISE_RUN) {
      groups.push({ kind: "noise", lines: run });
    } else {
      for (const l of run) groups.push({ kind: "line", line: l });
    }
    run = [];
  };
  for (const line of lines) {
    if (line.noise) {
      run.push(line);
    } else {
      flush();
      groups.push({ kind: "line", line });
    }
  }
  flush();
  return groups;
}

function LogLineRow({ line }: { line: ParsedLogLine }) {
  return (
    <li className="step-log-line" value={line.n}>
      <span className="step-log-ln" aria-hidden="true">
        {line.n}
      </span>
      <span className="step-log-row">
        {line.ts && (
          <span className="step-log-ts" title={line.ts}>
            {shortenTimestamp(line.ts)}
          </span>
        )}
        <span className={SEVERITY_CLASS[line.severity]}>
          {line.body || "\u00A0"}
        </span>
      </span>
    </li>
  );
}

/**
 * Premium CI-style log viewer for a single orchestrator step.
 *
 * Improvements over the original renderer:
 *  - Structured parse (parseStepLog) instead of a fragile indexOf("---").
 *  - Toolbar with error/warning counts, an "issues only" filter, in-log
 *    search, copy and download.
 *  - Low-level credential / HTTP chatter folds into a <details> so genuine
 *    progress lines stay visible (signal over noise).
 *  - Timestamps shortened to HH:MM:SS (full value on hover) so the body is
 *    not pushed off-screen.
 *  - Semantic <ol>/<li> list with a polite live region while the step is
 *    still active, so assistive tech is notified of streamed lines.
 */
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
  const [filter, setFilter] = useState<LogFilter>("all");
  const [query, setQuery] = useState("");
  // Reader preferences (local to this step's log block):
  //  - wrap: soft-wrap long lines (default) vs. horizontal scroll;
  //  - fontStep: index into FONT_SIZE_STEPS_PX;
  //  - showTs: show/hide the per-line timestamp column.
  const [wrap, setWrap] = useState(true);
  const [fontStep, setFontStep] = useState(DEFAULT_FONT_STEP);
  const [showTs, setShowTs] = useState(true);
  const fontSizePx = FONT_SIZE_STEPS_PX[fontStep] ?? FONT_SIZE_STEPS_PX[DEFAULT_FONT_STEP];

  const { summary, detail, counts } = useMemo(() => {
    const lines = parseStepLog(log);
    const split = splitSummaryDetail(lines);
    return {
      summary: split.summary,
      detail: split.detail,
      counts: countIssues(lines),
    };
  }, [log]);

  const summaryText = summary.map((l) => l.body).join("\n").trim();
  const trimmedQuery = query.trim().toLowerCase();
  const filtering = filter === "issues" || trimmedQuery.length > 0;

  const filteredDetail = useMemo(() => {
    if (!filtering) return detail;
    return detail.filter((l) => {
      if (filter === "issues" && l.severity !== "error" && l.severity !== "warn") {
        return false;
      }
      if (trimmedQuery && !l.body.toLowerCase().includes(trimmedQuery)) {
        return false;
      }
      return true;
    });
  }, [detail, filter, filtering, trimmedQuery]);

  // Noise folding only applies to the unfiltered view; when a user filters or
  // searches they want the matching lines flat, not hidden behind a fold.
  const groups = useMemo<RenderGroup[]>(
    () =>
      filtering
        ? filteredDetail.map((line) => ({ kind: "line" as const, line }))
        : groupDetail(detail),
    [detail, filteredDetail, filtering],
  );

  const copyLog = () => {
    navigator.clipboard.writeText(log).catch(() => {});
    flashCopied(true);
  };

  const downloadLog = () => {
    const blob = new Blob([log], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${stepKey}-log.txt`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  const hasDetail = detail.length > 0;
  const hasIssues = counts.error > 0 || counts.warn > 0;
  const listId = `step-log-${stepKey}`;

  return (
    <div
      className="step-log-block"
      data-state={state}
      data-wrap={wrap ? "on" : "off"}
      data-ts={showTs ? "on" : "off"}
      id={listId}
      style={{ ["--step-log-fs" as string]: `${fontSizePx}px` }}
    >
      <div className="step-log-summary">
        <span>{summaryText || (hasDetail ? "" : "\u00A0")}</span>
        <div className="step-log-tools">
          {hasIssues && (
            <span className="step-log-counts" aria-hidden="true">
              {counts.error > 0 && (
                <span className="step-log-count step-log-count--error">
                  <XCircle size={11} strokeWidth={2} />
                  {counts.error}
                </span>
              )}
              {counts.warn > 0 && (
                <span className="step-log-count step-log-count--warn">
                  <AlertTriangle size={11} strokeWidth={2} />
                  {counts.warn}
                </span>
              )}
            </span>
          )}
          <button
            className="step-log-copy"
            onClick={copyLog}
            title="Copy full log to clipboard"
          >
            {copied ? (
              <Check size={11} strokeWidth={2} />
            ) : (
              <Copy size={11} strokeWidth={1.5} />
            )}
            <span>{copied ? "Copied" : "Copy"}</span>
          </button>
          <button
            className="step-log-copy"
            onClick={downloadLog}
            title="Download full log as a text file"
          >
            <Download size={11} strokeWidth={1.5} />
            <span>Download</span>
          </button>
        </div>
      </div>

      {hasDetail && (
        <div className="step-log-detail">
          {(detail.length > 4 || hasIssues) && (
            <div className="step-log-controls">
              <div
                className="step-log-filter"
                role="group"
                aria-label="Filter log lines"
              >
                <button
                  type="button"
                  className="step-log-chip"
                  aria-pressed={filter === "all"}
                  onClick={() => setFilter("all")}
                >
                  All
                </button>
                <button
                  type="button"
                  className="step-log-chip"
                  aria-pressed={filter === "issues"}
                  disabled={!hasIssues}
                  onClick={() => setFilter("issues")}
                >
                  Issues
                  {counts.error + counts.warn > 0
                    ? ` (${counts.error + counts.warn})`
                    : ""}
                </button>
              </div>
              <label className="step-log-search">
                <Search size={12} strokeWidth={1.5} aria-hidden="true" />
                <input
                  type="search"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Filter lines…"
                  aria-label="Search log lines"
                  spellCheck={false}
                />
              </label>
              <div className="step-log-view" role="group" aria-label="Log view options">
                <button
                  type="button"
                  className="step-log-chip step-log-chip--icon"
                  aria-pressed={!wrap}
                  title={wrap ? "Disable line wrap (scroll horizontally)" : "Enable line wrap"}
                  onClick={() => setWrap((v) => !v)}
                >
                  <WrapText size={12} strokeWidth={1.6} aria-hidden="true" />
                  <span className="sr-only">Toggle line wrap</span>
                </button>
                <button
                  type="button"
                  className="step-log-chip step-log-chip--icon"
                  aria-pressed={!showTs}
                  title={showTs ? "Hide timestamps" : "Show timestamps"}
                  onClick={() => setShowTs((v) => !v)}
                >
                  <Clock size={12} strokeWidth={1.6} aria-hidden="true" />
                  <span className="sr-only">Toggle timestamps</span>
                </button>
                <span className="step-log-fontsize">
                  <button
                    type="button"
                    className="step-log-chip step-log-chip--icon"
                    title="Smaller text"
                    disabled={fontStep <= 0}
                    onClick={() => setFontStep((s) => Math.max(0, s - 1))}
                  >
                    <Minus size={12} strokeWidth={1.6} aria-hidden="true" />
                    <span className="sr-only">Decrease log font size</span>
                  </button>
                  <button
                    type="button"
                    className="step-log-chip step-log-chip--icon"
                    title="Larger text"
                    disabled={fontStep >= FONT_SIZE_STEPS_PX.length - 1}
                    onClick={() =>
                      setFontStep((s) => Math.min(FONT_SIZE_STEPS_PX.length - 1, s + 1))
                    }
                  >
                    <Plus size={12} strokeWidth={1.6} aria-hidden="true" />
                    <span className="sr-only">Increase log font size</span>
                  </button>
                </span>
              </div>
            </div>
          )}
          <ol
            className="step-log-lines"
            role="log"
            aria-label={`${stepKey} step log`}
            aria-live={state === "active" ? "polite" : "off"}
            aria-relevant="additions text"
          >
            {groups.map((group, gi) =>
              group.kind === "line" ? (
                <LogLineRow key={`${stepKey}-${group.line.n}`} line={group.line} />
              ) : (
                <li className="step-log-noise" key={`${stepKey}-noise-${gi}`}>
                  <details>
                    <summary>
                      {group.lines.length} lines of credential / HTTP detail
                    </summary>
                    <ol className="step-log-lines step-log-lines--nested">
                      {group.lines.map((line) => (
                        <LogLineRow key={`${stepKey}-${line.n}`} line={line} />
                      ))}
                    </ol>
                  </details>
                </li>
              ),
            )}
            {filtering && filteredDetail.length === 0 && (
              <li className="step-log-empty">No lines match the current filter.</li>
            )}
          </ol>
        </div>
      )}
    </div>
  );
}
