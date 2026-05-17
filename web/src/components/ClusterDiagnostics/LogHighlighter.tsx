import type React from "react";

/**
 * Log syntax highlighter — lnav-style colorized log output.
 *
 * Single-responsibility: take raw log text, return JSX with consistent
 * tinting per token class. No data fetching, no scrolling, no chrome.
 */

const LOG_COLORS = {
  timestamp: "#6cb6ff",
  error: "#f47067",
  warn: "#f0c674",
  info: "#57ab5a",
  debug: "#986ee2",
  number: "#d2a8ff",
  ip: "#6cb6ff",
  path: "#96d0ff",
  key: "#e3b341",
  string: "#a5d6ff",
  dim: "#545d68",
} as const;

// Pre-compiled regex patterns to avoid re-creating per line.
const LOG_ERROR_RE = /\b(error|fatal|critical|panic|exception|fail(ed|ure)?)\b/i;
const LOG_WARN_RE = /\b(warn(ing)?)\b/i;
const LOG_TOKEN_RE =
  /(\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)|(\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d+)?\b)|(\b(?:ERROR|FATAL|CRITICAL|PANIC|EXCEPTION)\b)|(\b(?:WARN(?:ING)?)\b)|(\b(?:INFO)\b)|(\b(?:DEBUG|TRACE)\b)|("[^"]*"|'[^']*')|(\/[\w./\-]+(?:\.\w+))|(\b\w+(?:[-_]\w+)*=)|(\b\d+(?:\.\d+)?(?:m|Mi|Gi|Ki|ms|s|%|ns|us|µs)?\b)/gi;

function highlightLine(line: string): React.ReactNode[] {
  const isError = LOG_ERROR_RE.test(line);
  const isWarn = !isError && LOG_WARN_RE.test(line);

  const pattern = LOG_TOKEN_RE;
  pattern.lastIndex = 0;

  const parts: React.ReactNode[] = [];
  let lastIdx = 0;
  let match: RegExpExecArray | null;
  let key = 0;

  while ((match = pattern.exec(line)) !== null) {
    if (match.index > lastIdx) {
      const before = line.slice(lastIdx, match.index);
      parts.push(
        <span
          key={key++}
          style={
            isError
              ? { color: "#ffa198" }
              : isWarn
                ? { color: "#e3b341", opacity: 0.85 }
                : undefined
          }
        >
          {before}
        </span>,
      );
    }

    const [fullMatch, ts, ip, err, warn, info, debug, str, path, kv, num] = match;
    let color = "#c9d1d9";
    let fontWeight: number | undefined;

    if (ts) color = LOG_COLORS.timestamp;
    else if (ip) color = LOG_COLORS.ip;
    else if (err) {
      color = LOG_COLORS.error;
      fontWeight = 700;
    } else if (warn) {
      color = LOG_COLORS.warn;
      fontWeight = 600;
    } else if (info) color = LOG_COLORS.info;
    else if (debug) color = LOG_COLORS.debug;
    else if (str) color = LOG_COLORS.string;
    else if (path) color = LOG_COLORS.path;
    else if (kv) color = LOG_COLORS.key;
    else if (num) color = LOG_COLORS.number;

    parts.push(
      <span key={key++} style={{ color, fontWeight }}>
        {fullMatch}
      </span>,
    );
    lastIdx = match.index + fullMatch!.length;
  }

  if (lastIdx < line.length) {
    const rest = line.slice(lastIdx);
    parts.push(
      <span
        key={key++}
        style={
          isError
            ? { color: "#ffa198" }
            : isWarn
              ? { color: "#e3b341", opacity: 0.85 }
              : undefined
        }
      >
        {rest}
      </span>,
    );
  }

  return parts;
}

export function LogHighlighter({ text }: { text: string }) {
  if (!text) return <span style={{ color: "var(--text-faint)" }}>(empty log)</span>;

  const lines = text.split("\n");
  return (
    <>
      {lines.map((line, i) => (
        <div key={i} style={{ minHeight: "1.7em", display: "flex" }}>
          <span
            style={{
              color: LOG_COLORS.dim,
              userSelect: "none",
              minWidth: 36,
              textAlign: "right",
              paddingRight: 12,
              fontSize: 9,
              lineHeight: "1.7em",
            }}
          >
            {i + 1}
          </span>
          <span style={{ whiteSpace: "pre-wrap", wordBreak: "break-all", flex: 1 }}>
            {highlightLine(line)}
          </span>
        </div>
      ))}
    </>
  );
}
