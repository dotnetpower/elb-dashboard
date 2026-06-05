/**
 * Structured parser for a single orchestrator step's raw log string.
 *
 * Responsibility: Turn the newline-joined log text that `buildStepLog` /
 * the SSE stream produce into a typed, testable model (timestamp + body +
 * severity + low-level-noise flag per line, plus a stable summary / detail
 * split and aggregate issue counts). This replaces the previous fragile
 * `log.indexOf("---")` heuristic in `StepLogBlock` and is the backbone for
 * severity filtering, noise folding, timestamp shortening and live-region
 * rendering.
 * Edit boundaries: Pure string → data transforms only. No React, no DOM, no
 * styling. Keep every export deterministic so `parseStepLog.test.ts` can
 * lock the contract.
 * Key entry points: `parseStepLog`, `splitSummaryDetail`, `countIssues`,
 * `shortenTimestamp`, `classifySeverity`, `isNoiseLine`.
 * Risky contracts: `StepLogBlock.tsx` renders directly off `ParsedLogLine`;
 * `severity` values map 1:1 to the `.step-log-text--*` CSS classes.
 * Validation: `cd web && npm test -- parseStepLog.test`.
 */

export type LogSeverity =
  | "error"
  | "warn"
  | "ok"
  | "info"
  | "header"
  | "cmd"
  | "blast"
  | "text";

export interface ParsedLogLine {
  /** 1-based line number in the original (pre-filter) log. */
  n: number;
  /** Extracted leading timestamp, or null when the line has none. */
  ts: string | null;
  /** Line content with the leading timestamp (if any) removed. */
  body: string;
  severity: LogSeverity;
  /** True for low-level credential / HTTP-pipeline chatter (foldable). */
  noise: boolean;
}

const ANSI_PATTERN = /\x1B\[[0-9;?]*[ -/]*[@-~]/g;
const TIMESTAMP_PATTERN =
  /^(\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\]?)\s*/;
const TIME_OF_DAY_PATTERN = /(\d{2}):(\d{2}):(\d{2})/;

// Low-level credential acquisition / HTTP request-response chatter that
// dwarfs the meaningful orchestrator progress lines. These are foldable by
// default so the signal (config written, queries split, submit ok) stays
// visible. Kept intentionally broad but anchored to azure-identity /
// azure-core pipeline vocabulary so genuine BLAST output is never folded.
const NOISE_PATTERN =
  /(ManagedIdentityCredential|ImdsCredential|EnvironmentCredential|DefaultAzureCredential|WorkloadIdentityCredential|AzureCliCredential|SharedTokenCacheCredential)|azure\.(identity|core\.pipeline)|Request URL:|Request method:|Request headers:|Response status:|Response headers:|No body was attached to the request|msi\/token|metadata\/identity\/oauth2|169\.254\.169\.254|REDACTED/i;
// Header-dictionary dump lines such as `    'User-Agent': 'azsdk-python-…'`.
const HEADER_DICT_PATTERN =
  /^\s*'(Authorization|User-Agent|Accept|Accept-Encoding|Content-Type|Content-Length|Connection|Host|Metadata|x-ms-[a-z0-9-]+|traceparent|client-request-id|return-client-request-id)'\s*:/i;

export function stripAnsi(value: string): string {
  return value.replace(ANSI_PATTERN, "");
}

/**
 * Reduce a verbose ISO/space timestamp to a compact `HH:MM:SS` so the log
 * body is not pushed off-screen by a 27-character prefix on every line.
 */
export function shortenTimestamp(ts: string): string {
  const m = ts.match(TIME_OF_DAY_PATTERN);
  return m ? `${m[1]}:${m[2]}:${m[3]}` : ts;
}

export function isNoiseLine(body: string): boolean {
  return NOISE_PATTERN.test(body) || HEADER_DICT_PATTERN.test(body);
}

export function classifySeverity(raw: string): LogSeverity {
  const line = raw.trimStart();
  if (!line) return "text";
  if (
    line.startsWith("ERROR") ||
    line.startsWith("FATAL") ||
    line.startsWith("✗") ||
    /\b(Traceback|panic:|Exception:|ContainerNotFound)\b/.test(line) ||
    /ErrorCode:|<Error>/.test(line)
  ) {
    return "error";
  }
  if (
    line.startsWith("WARNING") ||
    line.startsWith("WARN") ||
    line.startsWith("⚠") ||
    /\b(deprecated|skipped)\b/i.test(line)
  ) {
    return "warn";
  }
  if (
    line.startsWith("✓") ||
    line.includes("EXIT_CODE=0") ||
    /\b(SUCCESS|Completed|completed successfully)\b/.test(line)
  ) {
    return "ok";
  }
  if (line.startsWith("---") || line.startsWith("===") || line.startsWith("###")) {
    return "header";
  }
  if (line.startsWith("$ ") || line.startsWith("> ") || line.startsWith("# ")) {
    return "cmd";
  }
  if (/^(INFO|DEBUG|TRACE|NOTE)[: ]/i.test(line)) {
    return "info";
  }
  if (
    /\b(BLAST RUNTIME|RUN END|Database:|Posted date:|Database size:|Number of sequences|Number of letters)\b/.test(
      line,
    ) ||
    /^(elastic-blast|kubectl|az |azcopy|blastn|blastp|blastx|tblastn|tblastx)\b/.test(line)
  ) {
    return "blast";
  }
  return "text";
}

export function parseLogLine(raw: string, n: number): ParsedLogLine {
  const cleaned = stripAnsi(raw);
  let body = cleaned;
  let ts: string | null = null;
  const match = body.match(TIMESTAMP_PATTERN);
  if (match) {
    ts = match[1];
    body = body.slice(match[0].length);
  }
  return {
    n,
    ts,
    body,
    severity: classifySeverity(body),
    noise: isNoiseLine(body),
  };
}

export function parseStepLog(log: string): ParsedLogLine[] {
  return log.split("\n").map((line, i) => parseLogLine(line, i + 1));
}

/**
 * Split parsed lines into a leading human-readable summary block and the
 * detailed remainder. The boundary is the first `header` line (`--- … ---`,
 * `=== … ===`, `### …`) which is how `buildStepLog` delimits its prologue
 * from console output. Falls back to "first line is the summary" when there
 * is no header and the log is longer than two lines — matching the previous
 * behaviour but without the brittle substring search.
 */
export function splitSummaryDetail(lines: ParsedLogLine[]): {
  summary: ParsedLogLine[];
  detail: ParsedLogLine[];
} {
  const headerIdx = lines.findIndex((l) => l.severity === "header");
  if (headerIdx > 0) {
    return { summary: lines.slice(0, headerIdx), detail: lines.slice(headerIdx) };
  }
  if (headerIdx === 0) {
    return { summary: [], detail: lines };
  }
  if (lines.length <= 2) {
    return { summary: lines, detail: [] };
  }
  return { summary: lines.slice(0, 1), detail: lines.slice(1) };
}

export function countIssues(lines: ParsedLogLine[]): {
  error: number;
  warn: number;
} {
  let error = 0;
  let warn = 0;
  for (const l of lines) {
    if (l.severity === "error") error += 1;
    else if (l.severity === "warn") warn += 1;
  }
  return { error, warn };
}
