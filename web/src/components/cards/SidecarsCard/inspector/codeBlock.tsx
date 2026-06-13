/**
 * Sidecar HTTP request inspector — copy button + body code block.
 *
 * `CopyActionButton` (clipboard with copied/failed feedback) and
 * `CodeBlock` (JSON/XML detection, loose re-formatting, and lightweight
 * token highlighting for request/response bodies).
 */

import { useEffect, useRef, useState } from "react";
import { Check, Copy } from "lucide-react";

async function writeClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard?.writeText(text);
    return true;
  } catch {
    const textArea = document.createElement("textarea");
    textArea.value = text;
    textArea.setAttribute("readonly", "");
    textArea.style.position = "fixed";
    textArea.style.left = "-9999px";
    textArea.style.top = "0";
    document.body.appendChild(textArea);
    textArea.select();
    try {
      return document.execCommand("copy");
    } catch {
      return false;
    } finally {
      document.body.removeChild(textArea);
    }
  }
}

export function CopyActionButton({
  value,
  label,
  title,
  iconSize = 10,
  style,
}: {
  value: string;
  label: string;
  title: string;
  iconSize?: number;
  style?: React.CSSProperties;
}) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const resetTimer = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
    };
  }, []);

  const isCopied = state === "copied";
  const isFailed = state === "failed";
  const text = isCopied ? "Copied" : isFailed ? "Failed" : label;
  const color = isCopied
    ? "var(--success)"
    : isFailed
      ? "var(--danger)"
      : "var(--text-primary)";
  const borderColor = isCopied
    ? "rgba(106, 214, 163, 0.55)"
    : isFailed
      ? "rgba(224, 123, 138, 0.55)"
      : "var(--border-weak)";
  const background = isCopied
    ? "rgba(106, 214, 163, 0.14)"
    : isFailed
      ? "rgba(224, 123, 138, 0.14)"
      : "rgba(255,255,255,0.04)";

  const handleClick = async () => {
    const ok = await writeClipboard(value);
    setState(ok ? "copied" : "failed");
    if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
    resetTimer.current = window.setTimeout(() => setState("idle"), 1200);
  };

  return (
    <button
      type="button"
      className="glass-button"
      title={state === "idle" ? title : text}
      aria-live="polite"
      onClick={() => void handleClick()}
      style={{
        padding: "2px 6px",
        minWidth: 58,
        fontSize: 10,
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        gap: 3,
        color,
        background,
        borderColor,
        ...style,
      }}
    >
      {isCopied ? <Check size={iconSize} /> : <Copy size={iconSize} />}
      {text}
    </button>
  );
}

type BodyLanguage = "json" | "xml" | "text";

export function CodeBlock({
  label,
  code,
  contentType,
}: {
  label: string;
  code: string;
  contentType?: string;
}) {
  const language = detectBodyLanguage(code, contentType);
  const displayCode = formatBody(code, language);
  return (
    <div>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          fontSize: 10,
          color: "var(--text-muted)",
          textTransform: "uppercase",
          letterSpacing: "0.06em",
          marginBottom: 4,
        }}
      >
        <span>
          {label}{" "}
          <span style={{ marginLeft: 6, color: "var(--text-faint)", fontWeight: 600 }}>
            {language.toUpperCase()}
          </span>
        </span>
        <CopyActionButton value={displayCode} label="Copy" title="Copy body" />
      </div>
      <pre
        style={{
          margin: 0,
          padding: 10,
          background: "rgba(0,0,0,0.25)",
          border: "1px solid var(--border-weak)",
          borderRadius: 6,
          fontSize: 11,
          color: "var(--text-primary)",
          whiteSpace: "pre-wrap",
          overflowWrap: "anywhere",
          wordBreak: "break-word",
        }}
      >
        {highlightBody(displayCode, language)}
      </pre>
    </div>
  );
}

function detectBodyLanguage(code: string, contentType?: string): BodyLanguage {
  const type = contentType?.toLowerCase() ?? "";
  const trimmed = code.trimStart();
  if (type.includes("json") || trimmed.startsWith("{") || trimmed.startsWith("["))
    return "json";
  if (
    type.includes("xml") ||
    trimmed.startsWith("<?xml") ||
    /^<[a-zA-Z_][\w:.-]*(\s|>|\/)/.test(trimmed)
  ) {
    return "xml";
  }
  return "text";
}

function formatBody(code: string, language: BodyLanguage): string {
  if (language === "json") {
    try {
      const parsed = JSON.parse(code);
      if (typeof parsed === "string" && /^[\s\r\n]*[\[{]/.test(parsed)) {
        return formatBody(parsed, "json");
      }
      return JSON.stringify(parsed, null, 2);
    } catch {
      return formatJsonLoose(code);
    }
  }
  if (language === "xml") return formatXml(code);
  return code;
}

function formatJsonLoose(code: string): string {
  let depth = 0;
  let inString = false;
  let escaped = false;
  let out = "";
  const indent = () => "  ".repeat(Math.max(0, depth));

  for (const char of code) {
    if (inString) {
      out += char;
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === '"') inString = false;
      continue;
    }

    if (char === '"') {
      inString = true;
      out += char;
      continue;
    }
    if (char === "{" || char === "[") {
      depth += 1;
      out += `${char}\n${indent()}`;
      continue;
    }
    if (char === "}" || char === "]") {
      depth = Math.max(0, depth - 1);
      out = out.trimEnd();
      out += `\n${indent()}${char}`;
      continue;
    }
    if (char === ",") {
      out += `,\n${indent()}`;
      continue;
    }
    if (char === ":") {
      out += ": ";
      continue;
    }
    if (/\s/.test(char)) {
      if (!out.endsWith(" ") && !out.endsWith("\n")) out += " ";
      continue;
    }
    out += char;
  }

  return out.trim();
}

function formatXml(code: string): string {
  const trimmed = code.trim();
  if (!trimmed.includes("><")) return code;
  let depth = 0;
  return trimmed
    .replace(/>\s*</g, "><")
    .replace(/</g, "\n<")
    .trim()
    .split("\n")
    .map((rawLine) => {
      const line = rawLine.trim();
      if (/^<\//.test(line)) depth = Math.max(0, depth - 1);
      const formatted = `${"  ".repeat(depth)}${line}`;
      if (/^<[^!?/][^>]*[^/]?>$/.test(line) && !/^<[^>]+>.*<\//.test(line)) depth += 1;
      return formatted;
    })
    .join("\n");
}

function highlightBody(code: string, language: BodyLanguage): React.ReactNode {
  if (language === "json") return renderJsonTokens(code);
  if (language === "xml") return renderXmlTokens(code);
  return code;
}

function renderJsonTokens(code: string): React.ReactNode {
  const tokenPattern =
    /("(?:\\.|[^"\\])*"(?=\s*:))|("(?:\\.|[^"\\])*")|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|([{}\[\],:])/g;
  const nodes: React.ReactNode[] = [];
  let cursor = 0;
  for (const match of code.matchAll(tokenPattern)) {
    const index = match.index ?? 0;
    if (index > cursor) nodes.push(code.slice(cursor, index));
    const token = match[0];
    let color = "var(--text-muted)";
    if (match[1]) color = "var(--accent)";
    else if (match[2]) color = "var(--success)";
    else if (/^-?\d/.test(token)) color = "var(--warning)";
    else if (token === "true" || token === "false") color = "var(--danger)";
    else if (token === "null") color = "var(--text-faint)";
    nodes.push(
      <span key={`${index}-${token}`} style={{ color }}>
        {token}
      </span>,
    );
    cursor = index + token.length;
  }
  if (cursor < code.length) nodes.push(code.slice(cursor));
  return nodes;
}

function renderXmlTokens(code: string): React.ReactNode {
  const tokenPattern =
    /(<!\[CDATA\[[\s\S]*?\]\]>|<!--[\s\S]*?-->|<\/?[A-Za-z_][\w:.-]*(?:\s+[A-Za-z_:][\w:.-]*(?:=(?:"[^"]*"|'[^']*'))?)*\s*\/?>|&[a-zA-Z0-9#]+;)/g;
  const nodes: React.ReactNode[] = [];
  let cursor = 0;
  for (const match of code.matchAll(tokenPattern)) {
    const index = match.index ?? 0;
    if (index > cursor) nodes.push(code.slice(cursor, index));
    const token = match[0];
    if (token.startsWith("<!--")) {
      nodes.push(
        <span key={`${index}-comment`} style={{ color: "var(--text-faint)" }}>
          {token}
        </span>,
      );
    } else if (token.startsWith("<![CDATA")) {
      nodes.push(
        <span key={`${index}-cdata`} style={{ color: "var(--warning)" }}>
          {token}
        </span>,
      );
    } else if (token.startsWith("&")) {
      nodes.push(
        <span key={`${index}-entity`} style={{ color: "var(--success)" }}>
          {token}
        </span>,
      );
    } else {
      nodes.push(renderXmlTag(token, index));
    }
    cursor = index + token.length;
  }
  if (cursor < code.length) nodes.push(code.slice(cursor));
  return nodes;
}

function renderXmlTag(tag: string, offset: number): React.ReactNode {
  const parts = tag.match(/(<\/?|\/?>|[A-Za-z_][\w:.-]*|=|"[^"]*"|'[^']*'|\s+)/g) ?? [
    tag,
  ];
  let tagNameSeen = false;
  return (
    <span key={`${offset}-tag`}>
      {parts.map((part, index) => {
        let color = "var(--text-muted)";
        if (part.startsWith("<") || part === ">" || part === "/>")
          color = "var(--text-muted)";
        else if (!tagNameSeen && /^\S+$/.test(part) && part !== "=") {
          color = "var(--accent)";
          tagNameSeen = true;
        } else if (part.startsWith('"') || part.startsWith("'")) color = "var(--success)";
        else if (part !== "=" && /^\S+$/.test(part)) color = "var(--warning)";
        return (
          <span key={`${offset}-${index}`} style={{ color }}>
            {part}
          </span>
        );
      })}
    </span>
  );
}
