/**
 * Sidecar HTTP request inspector (Variant A — the shipped design).
 *
 * Renders the live per-request detail panel on the `api` sidecar card:
 *   • a latency/status scatter chart ("is anything slow / failing now?")
 *   • a request table ("which URL / who / how long / what status?")
 *   • a drill-down ("what headers / body did that call carry?")
 *
 * Consumed by `HttpInspectorPanel.tsx`, which feeds it real captured
 * traffic (`InspectorRequest[]`) from `GET /api/monitor/sidecar-requests`.
 * The backend already masks `Authorization` / `Cookie` / `X-Api-Key` and
 * caps bodies — see `api/services/request_metrics.py`. This module is
 * pure presentation: it does not fetch.
 *
 * Provenance: extracted verbatim from the retired
 * `pages/mockups/SidecarInspectorMockups.tsx` design-exploration page
 * (issue #24). Only Variant A and its helpers are kept; the two
 * unshipped variants and the demo fixture were removed.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertOctagon,
  Check,
  ChevronRight,
  Copy,
  Pause,
  Play,
  Search,
  X,
} from "lucide-react";

/* -------------------------------------------------------------------- */
/* Shared types                                                         */
/* -------------------------------------------------------------------- */

interface MockReq {
  id: string;
  ts: number; // epoch ms
  method: "GET" | "POST" | "DELETE" | "PUT" | string;
  path: string;
  status: number;
  durationMs: number;
  caller: string; // UPN or "anonymous"
  clientIp: string;
  requestId: string;
  requestHeaders: Record<string, string>;
  requestBody?: string;
  responseHeaders: Record<string, string>;
  responseBody?: string;
  responseBytes: number;
  degraded?: boolean;
  degradedReasons?: string[];
}

// Re-exported under a non-mockup name for the production HttpInspectorPanel.
// The shape is an internal contract between this file and that consumer —
// keep them in sync when changing fields.
export type InspectorRequest = MockReq;

/* -------------------------------------------------------------------- */
/* Shared atoms                                                         */
/* -------------------------------------------------------------------- */

function statusTone(code: number): {
  fg: string;
  bg: string;
  ring: string;
  label: string;
} {
  if (code >= 500)
    return {
      fg: "var(--danger)",
      bg: "rgba(224, 123, 138, 0.14)",
      ring: "rgba(224, 123, 138, 0.55)",
      label: "5xx",
    };
  if (code >= 400)
    return {
      fg: "var(--warning)",
      bg: "rgba(240, 198, 116, 0.14)",
      ring: "rgba(240, 198, 116, 0.55)",
      label: "4xx",
    };
  if (code >= 300)
    return {
      fg: "var(--accent)",
      bg: "rgba(122, 167, 255, 0.14)",
      ring: "rgba(122, 167, 255, 0.55)",
      label: "3xx",
    };
  return {
    fg: "var(--success)",
    bg: "rgba(106, 214, 163, 0.14)",
    ring: "rgba(106, 214, 163, 0.55)",
    label: "2xx",
  };
}

function methodTone(m: string): string {
  if (m === "POST") return "var(--accent)";
  if (m === "DELETE") return "var(--danger)";
  if (m === "PUT") return "var(--warning)";
  return "var(--text-muted)";
}

function fmtTime(ts: number): string {
  const d = new Date(ts);
  return (
    d.getHours().toString().padStart(2, "0") +
    ":" +
    d.getMinutes().toString().padStart(2, "0") +
    ":" +
    d.getSeconds().toString().padStart(2, "0")
  );
}
function fmtAgo(ts: number, now: number): string {
  const s = Math.floor((now - ts) / 1000);
  if (s < 60) return `${s}s ago`;
  return `${Math.floor(s / 60)}m ${s % 60}s ago`;
}
function fmtMs(ms: number): string {
  if (ms >= 1000) return (ms / 1000).toFixed(2) + "s";
  return Math.round(ms) + "ms";
}
function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}
function niceLatencyFloor(ms: number): number {
  const candidates = [
    1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000, 60000,
  ];
  for (let i = candidates.length - 1; i >= 0; i--) {
    if (candidates[i] <= ms) return candidates[i];
  }
  return candidates[0];
}
function niceLatencyCeil(ms: number): number {
  const candidates = [
    1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000, 60000,
  ];
  return (
    candidates.find((candidate) => candidate >= ms) ?? candidates[candidates.length - 1]
  );
}
function latencyTicks(minMs: number, maxMs: number): number[] {
  const candidates = [
    1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000, 60000,
  ];
  const ticks = candidates.filter(
    (candidate) => candidate >= minMs && candidate <= maxMs,
  );
  if (ticks.length <= 6) return ticks;
  const step = Math.ceil(ticks.length / 6);
  const sampled = ticks.filter((_, index) => index % step === 0);
  const last = ticks[ticks.length - 1];
  return sampled.includes(last) ? sampled : [...sampled, last];
}
function latencyTone(ms: number): string {
  if (ms >= 2000) return "var(--danger)";
  if (ms >= 500) return "var(--warning)";
  if (ms >= 200) return "var(--text-primary)";
  return "var(--success)";
}

const DEGRADED_COLOR = "#e69b82";
const DEGRADED_BG = "rgba(230, 155, 130, 0.14)";
const DEGRADED_RING = "rgba(230, 155, 130, 0.58)";

function requestTone(req: MockReq): ReturnType<typeof statusTone> {
  if (req.degraded && req.status < 400) {
    return {
      fg: DEGRADED_COLOR,
      bg: DEGRADED_BG,
      ring: DEGRADED_RING,
      label: "degraded",
    };
  }
  return statusTone(req.status);
}

function fmtBytes(b: number): string {
  if (b > 1024) return (b / 1024).toFixed(1) + " KiB";
  return b + " B";
}

function trianglePoints(cx: number, cy: number, radius: number): string {
  const height = radius * 1.75;
  return [
    `${cx},${cy - height / 2}`,
    `${cx - radius},${cy + height / 2}`,
    `${cx + radius},${cy + height / 2}`,
  ].join(" ");
}

function StatusPill({ code }: { code: number }) {
  const t = statusTone(code);
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding: "1px 6px",
        borderRadius: 4,
        fontSize: 10,
        fontWeight: 600,
        background: t.bg,
        color: t.fg,
        border: "1px solid " + t.ring,
        fontVariantNumeric: "tabular-nums",
      }}
    >
      {code}
    </span>
  );
}

function DegradedPill() {
  return (
    <span
      title="HTTP request succeeded, but the response reports a degraded domain state"
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding: "1px 6px",
        borderRadius: 4,
        fontSize: 10,
        fontWeight: 700,
        color: DEGRADED_COLOR,
        background: DEGRADED_BG,
        border: `1px solid ${DEGRADED_RING}`,
      }}
    >
      Degraded
    </span>
  );
}

function MethodPill({ method }: { method: string }) {
  return (
    <span
      style={{
        display: "inline-block",
        minWidth: 44,
        padding: "1px 5px",
        textAlign: "center",
        fontSize: 10,
        fontWeight: 700,
        letterSpacing: "0.04em",
        color: methodTone(method),
        background: "rgba(255,255,255,0.04)",
        border: "1px solid var(--border-weak)",
        borderRadius: 3,
        fontVariantNumeric: "tabular-nums",
      }}
    >
      {method}
    </span>
  );
}

/* -------------------------------------------------------------------- */
/* Detail content (shared between drawer / modal / accordion)           */
/* -------------------------------------------------------------------- */

function DetailContent({ r }: { r: MockReq }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <Row label="Request ID" value={<code>{r.requestId}</code>} />
      <Row label="Time" value={`${fmtTime(r.ts)} · ${fmtAgo(r.ts, Date.now())}`} />
      <Row label="Caller" value={r.caller} />
      <Row label="Client IP" value={<code>{r.clientIp}</code>} />
      <Row
        label="Status / Duration"
        value={
          <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
            <StatusPill code={r.status} />
            {r.degraded && <DegradedPill />}
            <span style={{ color: "var(--text-muted)" }}>·</span>
            <span style={{ fontVariantNumeric: "tabular-nums" }}>
              {fmtMs(r.durationMs)}
            </span>
            <span style={{ color: "var(--text-muted)" }}>·</span>
            <span style={{ color: "var(--text-muted)" }}>
              {fmtBytes(r.responseBytes)}
            </span>
          </span>
        }
      />
      {r.degraded && r.degradedReasons && r.degradedReasons.length > 0 && (
        <Row label="Degraded reason" value={r.degradedReasons.join(" · ")} />
      )}
      <SectionHeader title="Request" />
      <KvBlock entries={r.requestHeaders} />
      {r.requestBody && (
        <CodeBlock
          label="Body"
          code={r.requestBody}
          contentType={headerValue(r.requestHeaders, "content-type")}
        />
      )}
      <SectionHeader title="Response" />
      <KvBlock entries={r.responseHeaders} />
      {r.responseBody && (
        <CodeBlock
          label="Body"
          code={r.responseBody}
          contentType={headerValue(r.responseHeaders, "content-type")}
        />
      )}
    </div>
  );
}

function headerValue(headers: Record<string, string>, name: string): string | undefined {
  const needle = name.toLowerCase();
  const found = Object.entries(headers).find(([key]) => key.toLowerCase() === needle);
  return found?.[1];
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 12, alignItems: "baseline" }}>
      <span style={{ width: 110, color: "var(--text-muted)", fontSize: 11 }}>
        {label}
      </span>
      <span style={{ fontSize: 12, color: "var(--text-primary)" }}>{value}</span>
    </div>
  );
}
function SectionHeader({ title }: { title: string }) {
  return (
    <div
      style={{
        marginTop: 8,
        fontSize: 10,
        textTransform: "uppercase",
        letterSpacing: "0.08em",
        color: "var(--text-muted)",
        borderBottom: "1px solid var(--border-weak)",
        paddingBottom: 4,
      }}
    >
      {title}
    </div>
  );
}
function KvBlock({ entries }: { entries: Record<string, string> }) {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "auto 1fr",
        gap: "3px 10px",
        fontSize: 11,
        fontFamily: "var(--mono)",
      }}
    >
      {Object.entries(entries).map(([k, v]) => (
        <div key={k} style={{ display: "contents" }}>
          <span style={{ color: "var(--text-muted)" }}>{k}:</span>
          <span style={{ color: "var(--text-primary)", wordBreak: "break-all" }}>
            {v}
          </span>
        </div>
      ))}
    </div>
  );
}

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

function CopyActionButton({
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

function CodeBlock({
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

/* ==================================================================== */
/* VARIANT A — Timeline scatter + right-side drawer                     */
/* ==================================================================== */

export function VariantA({ data }: { data: MockReq[] }) {
  const [graphSelected, setGraphSelected] = useState<MockReq | null>(null);
  const [tableSelected, setTableSelected] = useState<MockReq | null>(null);
  const [paused, setPaused] = useState(false);
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [query, setQuery] = useState("");
  const [tableLimit, setTableLimit] = useState(25);
  const tableDetailRef = useRef<HTMLDivElement | null>(null);

  // Esc closes drawer
  useEffect(() => {
    if (!graphSelected) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setGraphSelected(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [graphSelected]);

  // Time window filter — anchored to the most recent sample's timestamp
  // so live + fixture data both stay visible. (Mockup fixtures were
  // generated against NOW at module load; live data is recent by
  // definition.)
  const windowMin = 5;
  const referenceTs = data.length > 0 ? Math.max(...data.map((d) => d.ts)) : Date.now();
  const windowStart = referenceTs - windowMin * 60_000;
  const windowed = useMemo(
    () => data.filter((d) => d.ts >= windowStart),
    [data, windowStart],
  );

  const counts = useMemo(() => {
    const c = { ok: 0, redirect: 0, client: 0, server: 0, degraded: 0 };
    for (const d of windowed) {
      if (d.degraded) c.degraded++;
      if (d.status >= 500) c.server++;
      else if (d.status >= 400) c.client++;
      else if (d.status >= 300) c.redirect++;
      else c.ok++;
    }
    return c;
  }, [windowed]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return windowed.filter((d) => {
      if (errorsOnly && d.status < 400 && !d.degraded) return false;
      if (
        q &&
        !d.path.toLowerCase().includes(q) &&
        !d.caller.toLowerCase().includes(q) &&
        !d.requestId.toLowerCase().includes(q) &&
        !String(d.status).includes(q) &&
        !(d.degraded && "degraded".includes(q)) &&
        !(d.degradedReasons ?? []).some((reason) => reason.toLowerCase().includes(q))
      ) {
        return false;
      }
      return true;
    });
  }, [windowed, errorsOnly, query]);

  // Reset paginated cap when filter set changes
  useEffect(() => {
    setTableLimit(25);
    setTableSelected(null);
  }, [errorsOnly, query, windowMin]);

  useEffect(() => {
    if (!tableSelected) return;
    const frameId = window.requestAnimationFrame(() => {
      tableDetailRef.current?.scrollIntoView({
        behavior: "smooth",
        block: "start",
        inline: "nearest",
      });
      tableDetailRef.current?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frameId);
  }, [tableSelected]);

  return (
    <div
      className="glass-card"
      style={{ padding: 14, position: "relative", overflow: "hidden" }}
    >
      <Header
        eyebrow="API sidecar"
        title="HTTP requests"
        right={
          <div
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              flexWrap: "wrap",
            }}
          >
            <LiveIndicator paused={paused} />
            <CountChips counts={counts} />
            <button
              type="button"
              className="glass-button"
              onClick={() => setErrorsOnly((v) => !v)}
              aria-pressed={errorsOnly}
              title={errorsOnly ? "Show all requests" : "Show only 4xx / 5xx / degraded"}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
                fontSize: 10,
                padding: "3px 7px",
                color: errorsOnly ? "var(--danger)" : undefined,
                borderColor: errorsOnly ? "var(--danger)" : undefined,
              }}
            >
              <AlertOctagon size={11} />
              Errors
            </button>
            <button
              type="button"
              className="glass-button"
              onClick={() => setPaused((p) => !p)}
              aria-pressed={paused}
              aria-label={paused ? "Resume live updates" : "Pause for review"}
              title={paused ? "Resume live updates" : "Pause for review"}
              style={{
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 10,
                padding: 5,
                width: 26,
                height: 22,
              }}
            >
              {paused ? <Play size={12} /> : <Pause size={12} />}
            </button>
          </div>
        }
      />
      <div style={{ position: "relative" }}>
        <ScatterChart
          data={filtered}
          windowStart={windowStart}
          windowEnd={referenceTs}
          onPick={setGraphSelected}
          selectedId={graphSelected?.id}
        />
        {paused && (
          <div
            style={{
              position: "absolute",
              top: 8,
              left: 12,
              padding: "2px 8px",
              fontSize: 10,
              fontWeight: 700,
              letterSpacing: "0.08em",
              color: "var(--warning)",
              border: "1px solid var(--warning)",
              borderRadius: 3,
              background: "rgba(240, 198, 116, 0.08)",
            }}
          >
            PAUSED
          </div>
        )}
      </div>
      <SearchBar
        value={query}
        onChange={setQuery}
        total={windowed.length}
        shown={filtered.length}
      />
      <TableA
        data={filtered}
        selectedId={tableSelected?.id}
        onPick={setTableSelected}
        limit={tableLimit}
        onShowMore={() => setTableLimit((n) => n + 50)}
      />
      {tableSelected && (
        <div
          ref={tableDetailRef}
          tabIndex={-1}
          style={{ scrollMarginTop: 12, outline: "none" }}
        >
          <InlineRequestDetail
            req={tableSelected}
            onClose={() => setTableSelected(null)}
          />
        </div>
      )}
      {graphSelected && (
        <Drawer onClose={() => setGraphSelected(null)} req={graphSelected} />
      )}
    </div>
  );
}

function SearchBar({
  value,
  onChange,
  total,
  shown,
}: {
  value: string;
  onChange: (v: string) => void;
  total: number;
  shown: number;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        margin: "0 0 8px 0",
        padding: "0 2px",
      }}
    >
      <div
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          padding: "4px 8px",
          flex: 1,
          background: "rgba(255,255,255,0.04)",
          border: "1px solid var(--border-weak)",
          borderRadius: 6,
        }}
      >
        <Search size={12} style={{ color: "var(--text-muted)" }} />
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="Filter by path, caller, request_id, status code…"
          aria-label="Filter requests"
          style={{
            flex: 1,
            background: "transparent",
            border: "none",
            outline: "none",
            color: "var(--text-primary)",
            fontSize: 11,
            fontFamily: "inherit",
          }}
        />
        {value && (
          <button
            type="button"
            onClick={() => onChange("")}
            aria-label="Clear filter"
            title="Clear"
            style={{
              background: "transparent",
              border: "none",
              color: "var(--text-muted)",
              cursor: "pointer",
              padding: 0,
              display: "inline-flex",
            }}
          >
            <X size={11} />
          </button>
        )}
      </div>
      <span
        style={{
          fontSize: 10,
          color: "var(--text-muted)",
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {shown === total ? `${total} requests` : `${shown} of ${total}`}
      </span>
    </div>
  );
}

function CountChips({
  counts,
}: {
  counts: {
    ok: number;
    redirect: number;
    client: number;
    server: number;
    degraded: number;
  };
}) {
  const items: { label: string; n: number; color: string }[] = [
    { label: "ok", n: counts.ok, color: statusTone(200).fg },
    { label: "3xx", n: counts.redirect, color: statusTone(304).fg },
    { label: "4xx", n: counts.client, color: statusTone(404).fg },
    { label: "5xx", n: counts.server, color: statusTone(500).fg },
    { label: "degraded", n: counts.degraded, color: DEGRADED_COLOR },
  ];
  return (
    <div
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding: "2px 6px",
        background: "rgba(255,255,255,0.04)",
        border: "1px solid var(--border-weak)",
        borderRadius: 4,
        fontSize: 10,
        fontVariantNumeric: "tabular-nums",
      }}
      title="2xx · 3xx · 4xx · 5xx · degraded counts in current window"
    >
      {items.map((it, i) => (
        <span
          key={it.label}
          style={{ display: "inline-flex", alignItems: "center", gap: 3 }}
        >
          {i > 0 && (
            <span style={{ color: "var(--text-faint, var(--text-muted))" }}>·</span>
          )}
          {it.label === "degraded" ? (
            <span
              style={{
                width: 0,
                height: 0,
                borderLeft: "4px solid transparent",
                borderRight: "4px solid transparent",
                borderBottom: `7px solid ${it.color}`,
                display: "inline-block",
              }}
            />
          ) : (
            <span
              style={{
                width: 6,
                height: 6,
                borderRadius: "50%",
                background: it.color,
                display: "inline-block",
              }}
            />
          )}
          <span style={{ color: "var(--text-primary)", fontWeight: 600 }}>{it.n}</span>
          <span style={{ color: "var(--text-muted)" }}>{it.label}</span>
        </span>
      ))}
    </div>
  );
}

function LiveIndicator({ paused }: { paused: boolean }) {
  return (
    <span
      role="status"
      aria-live="polite"
      title={
        paused
          ? "Stream paused — click Resume to continue"
          : "Live — capturing every request through the api sidecar"
      }
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        padding: "2px 7px",
        background: "rgba(255,255,255,0.04)",
        border: "1px solid var(--border-weak)",
        borderRadius: 4,
        fontSize: 10,
        fontWeight: 600,
        letterSpacing: "0.06em",
        color: paused ? "var(--warning)" : "var(--success)",
      }}
    >
      <span
        style={{
          width: 7,
          height: 7,
          borderRadius: "50%",
          background: paused ? "var(--warning)" : "var(--success)",
          boxShadow: paused ? "none" : "0 0 0 0 rgba(106,214,163,0.7)",
          animation: paused ? "none" : "livePulse 1.6s infinite",
        }}
      />
      {paused ? "PAUSED" : "LIVE"}
    </span>
  );
}

function ScatterChart({
  data,
  windowStart,
  windowEnd,
  onPick,
  selectedId,
}: {
  data: MockReq[];
  windowStart: number;
  windowEnd: number;
  onPick: (r: MockReq) => void;
  selectedId?: string;
}) {
  const W = 880;
  const H = 220;
  const PAD = { l: 58, r: 16, t: 16, b: 44 };
  const POINT_EDGE_GAP = 8;
  const innerW = W - PAD.l - PAD.r;
  const innerH = H - PAD.t - PAD.b;
  // Anchor the time axis to the explicit window so the SLA line and ticks
  // stay stable as the user filters/searches (the data subset shouldn't
  // squish all dots into the right edge).
  const minTs = windowStart;
  const maxTs = windowEnd;
  const tRange = maxTs - minTs || 60_000;
  const durations = data.map((item) => Math.max(1, item.durationMs));
  const observedMin = durations.length > 0 ? Math.min(...durations) : 5;
  const observedMax = durations.length > 0 ? Math.max(...durations) : 3000;
  const yDomainMin = niceLatencyFloor(Math.max(1, observedMin * 0.75));
  const yDomainMax = Math.max(
    niceLatencyCeil(Math.max(observedMax * 1.18, yDomainMin * 2)),
    yDomainMin * 2,
  );
  const yMax = Math.log10(yDomainMax);
  const yMin = Math.log10(yDomainMin);
  const xOf = (ts: number) => PAD.l + ((ts - minTs) / tRange) * innerW;
  const pointXOf = (ts: number) =>
    clamp(xOf(ts), PAD.l + POINT_EDGE_GAP, W - PAD.r - POINT_EDGE_GAP);
  const yOf = (ms: number) => {
    const lv = Math.log10(Math.max(yDomainMin, Math.min(yDomainMax, ms)));
    return PAD.t + (1 - (lv - yMin) / (yMax - yMin)) * innerH;
  };
  const pointYOf = (ms: number) =>
    clamp(yOf(ms), PAD.t + POINT_EDGE_GAP, H - PAD.b - POINT_EDGE_GAP);

  const yTicks = latencyTicks(yDomainMin, yDomainMax);
  const xTickCount = 6;
  const xTicks = Array.from(
    { length: xTickCount },
    (_, i) => minTs + (i / (xTickCount - 1)) * tRange,
  );

  const wrapRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<{ r: MockReq; x: number; y: number } | null>(null);

  const positionFromEvent = (e: React.MouseEvent<SVGElement>, r: MockReq) => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const rect = wrap.getBoundingClientRect();
    setHover({ r, x: e.clientX - rect.left, y: e.clientY - rect.top });
  };

  return (
    <div ref={wrapRef} style={{ marginTop: 10, marginBottom: 12, position: "relative" }}>
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ display: "block" }}>
        {/* brighter plot background panel */}
        <rect
          x={PAD.l}
          y={PAD.t}
          width={innerW}
          height={innerH}
          fill="rgba(255,255,255,0.07)"
          stroke="var(--border-weak)"
          strokeWidth={0.5}
          rx={6}
        />
        {/* y gridlines */}
        {yTicks.map((y) => (
          <line
            key={`yg-${y}`}
            x1={PAD.l}
            x2={W - PAD.r}
            y1={yOf(y)}
            y2={yOf(y)}
            stroke="rgba(255,255,255,0.07)"
            strokeWidth={0.5}
          />
        ))}
        {/* x gridlines */}
        {xTicks.map((t, i) => (
          <line
            key={`xg-${i}`}
            x1={xOf(t)}
            x2={xOf(t)}
            y1={PAD.t}
            y2={H - PAD.b}
            stroke="rgba(255,255,255,0.05)"
            strokeWidth={0.5}
          />
        ))}
        {/* SLA reference */}
        {yDomainMin <= 2000 && yDomainMax >= 2000 && (
          <>
            <line
              x1={PAD.l}
              x2={W - PAD.r}
              y1={yOf(2000)}
              y2={yOf(2000)}
              stroke="var(--danger)"
              strokeDasharray="4 3"
              strokeWidth={1}
              opacity={0.6}
            >
              <title>
                SLA target — requests above this line breach the 2 s p95 budget
              </title>
            </line>
            <text
              x={W - PAD.r - 6}
              y={yOf(2000) - 4}
              fill="var(--danger)"
              fontSize="9"
              textAnchor="end"
            >
              SLA 2000 ms
            </text>
          </>
        )}
        {/* y axis line */}
        <line
          x1={PAD.l}
          x2={PAD.l}
          y1={PAD.t}
          y2={H - PAD.b}
          stroke="var(--text-muted)"
          strokeWidth={1}
        />
        {/* x axis line */}
        <line
          x1={PAD.l}
          x2={W - PAD.r}
          y1={H - PAD.b}
          y2={H - PAD.b}
          stroke="var(--text-muted)"
          strokeWidth={1}
        />
        {/* y tick marks + labels */}
        {yTicks.map((y) => (
          <g key={`yt-${y}`}>
            <line
              x1={PAD.l - 4}
              x2={PAD.l}
              y1={yOf(y)}
              y2={yOf(y)}
              stroke="var(--text-muted)"
              strokeWidth={1}
            />
            <text
              x={PAD.l - 7}
              y={yOf(y) + 3}
              fill="var(--text-muted)"
              fontSize="9"
              textAnchor="end"
            >
              {y}
            </text>
          </g>
        ))}
        {/* x tick marks + labels */}
        {xTicks.map((t, i) => (
          <g key={`xt-${i}`}>
            <line
              x1={xOf(t)}
              x2={xOf(t)}
              y1={H - PAD.b}
              y2={H - PAD.b + 4}
              stroke="var(--text-muted)"
              strokeWidth={1}
            />
            <text
              x={xOf(t)}
              y={H - PAD.b + 14}
              fill="var(--text-muted)"
              fontSize="9"
              textAnchor="middle"
            >
              {fmtTime(t)}
            </text>
          </g>
        ))}
        {/* axis titles */}
        <text
          x={-(PAD.t + innerH / 2)}
          y={14}
          fill="var(--text-muted)"
          fontSize="10"
          textAnchor="middle"
          transform="rotate(-90)"
        >
          Latency (ms · log scale)
        </text>
        <text
          x={PAD.l + innerW / 2}
          y={H - 4}
          fill="var(--text-muted)"
          fontSize="10"
          textAnchor="middle"
        >
          Time (last {windowMinLabel(windowStart, windowEnd)})
        </text>
        {/* dots */}
        {data.length === 0 && (
          <text
            x={PAD.l + innerW / 2}
            y={PAD.t + innerH / 2}
            fill="var(--text-muted)"
            fontSize="11"
            textAnchor="middle"
          >
            No requests in selected window
          </text>
        )}
        {hover && (
          <g pointerEvents="none">
            <line
              x1={pointXOf(hover.r.ts)}
              x2={pointXOf(hover.r.ts)}
              y1={PAD.t}
              y2={H - PAD.b}
              stroke="rgba(255,255,255,0.18)"
              strokeWidth={0.6}
              strokeDasharray="2 3"
            />
            <line
              x1={PAD.l}
              x2={W - PAD.r}
              y1={pointYOf(hover.r.durationMs)}
              y2={pointYOf(hover.r.durationMs)}
              stroke="rgba(255,255,255,0.18)"
              strokeWidth={0.6}
              strokeDasharray="2 3"
            />
          </g>
        )}
        {data.map((d) => {
          const tone = requestTone(d);
          const isSelected = d.id === selectedId;
          const isHovered = hover?.r.id === d.id;
          const is5xx = d.status >= 500;
          const isDegraded = Boolean(d.degraded && d.status < 400);
          const baseR = is5xx ? 4 : 3;
          const r = isHovered || isSelected ? baseR + 2 : baseR;
          const cx = pointXOf(d.ts);
          const cy = pointYOf(d.durationMs);
          return (
            <g key={d.id}>
              {isSelected && !isDegraded && (
                <circle
                  cx={cx}
                  cy={cy}
                  r={r + 4}
                  fill="none"
                  stroke={tone.fg}
                  strokeWidth={1.6}
                  opacity={0.85}
                />
              )}
              {isSelected && isDegraded && (
                <polygon
                  points={trianglePoints(cx, cy, r + 7)}
                  fill="none"
                  stroke={tone.fg}
                  strokeWidth={1.6}
                  opacity={0.85}
                />
              )}
              {is5xx && (
                /* contrasting halo for server errors so they pop above 2xx noise */
                <circle
                  cx={cx}
                  cy={cy}
                  r={r + 2}
                  fill="none"
                  stroke={tone.fg}
                  strokeWidth={1}
                  opacity={0.5}
                />
              )}
              {isDegraded ? (
                <polygon
                  points={trianglePoints(cx, cy, r + 1)}
                  fill={tone.fg}
                  opacity={isHovered || isSelected ? 1 : 0.88}
                  stroke={isHovered ? "#ffffff" : DEGRADED_RING}
                  strokeWidth={isHovered ? 1 : 0.8}
                  style={{ cursor: "pointer" }}
                  onMouseEnter={(e) => positionFromEvent(e, d)}
                  onMouseMove={(e) => positionFromEvent(e, d)}
                  onMouseLeave={() => setHover(null)}
                  onClick={() => onPick(d)}
                />
              ) : (
                <circle
                  cx={cx}
                  cy={cy}
                  r={r}
                  fill={tone.fg}
                  opacity={isHovered || isSelected ? 1 : 0.85}
                  stroke={isHovered ? "#ffffff" : "none"}
                  strokeWidth={isHovered ? 1 : 0}
                  style={{ cursor: "pointer" }}
                  onMouseEnter={(e) => positionFromEvent(e, d)}
                  onMouseMove={(e) => positionFromEvent(e, d)}
                  onMouseLeave={() => setHover(null)}
                  onClick={() => onPick(d)}
                />
              )}
            </g>
          );
        })}
      </svg>
      {hover &&
        (() => {
          const wrap = wrapRef.current;
          const wrapW = wrap?.clientWidth ?? 800;
          const wrapH = wrap?.clientHeight ?? 240;
          const TIP_W = 260;
          const TIP_H = 124;
          // Flip horizontally if cursor is past the right midline (so tip
          // never sits on top of dot or clips off the right edge).
          const flipLeft = hover.x + 14 + TIP_W > wrapW;
          const left = flipLeft
            ? Math.max(hover.x - TIP_W - 14, 4)
            : Math.min(hover.x + 14, wrapW - TIP_W - 4);
          // Flip vertically if cursor is in the bottom half (so tip rises above).
          const flipUp = hover.y + TIP_H + 12 > wrapH;
          const top = flipUp
            ? Math.max(hover.y - TIP_H - 12, 4)
            : Math.min(hover.y + 14, wrapH - TIP_H - 4);
          return (
            <div
              role="tooltip"
              style={{
                position: "absolute",
                pointerEvents: "none",
                left,
                top,
                background: "rgba(10,14,24,0.94)",
                border: "1px solid var(--border-weak)",
                borderRadius: 8,
                padding: "8px 10px",
                fontSize: 11,
                color: "var(--text-primary)",
                boxShadow: "0 6px 22px rgba(0,0,0,0.45)",
                minWidth: 220,
                maxWidth: TIP_W,
                zIndex: 5,
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  marginBottom: 4,
                }}
              >
                <MethodPill method={hover.r.method} />
                <StatusPill code={hover.r.status} />
                {hover.r.degraded && <DegradedPill />}
                <span
                  style={{
                    marginLeft: "auto",
                    color: latencyTone(hover.r.durationMs),
                    fontSize: 10,
                    fontWeight: 600,
                    fontVariantNumeric: "tabular-nums",
                  }}
                >
                  {fmtMs(hover.r.durationMs)}
                </span>
              </div>
              <div
                style={{
                  fontFamily: "var(--font-mono, monospace)",
                  fontSize: 11,
                  color: "var(--text-primary)",
                  wordBreak: "break-all",
                  marginBottom: 4,
                  lineHeight: 1.35,
                }}
              >
                {hover.r.path}
              </div>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  gap: 8,
                  fontSize: 10,
                  color: "var(--text-muted)",
                }}
              >
                <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                  {hover.r.caller}
                </span>
                <span>{fmtTime(hover.r.ts)}</span>
              </div>
              <div
                style={{
                  marginTop: 5,
                  fontSize: 10,
                  color: "var(--text-faint, var(--text-muted))",
                  borderTop: "1px solid var(--border-weak)",
                  paddingTop: 4,
                }}
              >
                Click point for full request / response
              </div>
            </div>
          );
        })()}
      <div
        style={{
          display: "flex",
          gap: 10,
          fontSize: 10,
          color: "var(--text-muted)",
          marginTop: 4,
        }}
      >
        <LegendDot color={statusTone(200).fg} label="2xx" />
        <LegendDot color={statusTone(304).fg} label="3xx" />
        <LegendDot color={statusTone(404).fg} label="4xx" />
        <LegendDot color={statusTone(500).fg} label="5xx" />
        <LegendDot color={DEGRADED_COLOR} label="degraded" shape="triangle" />
        <span style={{ marginLeft: "auto" }}>
          {data.length} samples · {fmtTime(windowStart)}–{fmtTime(windowEnd)}
        </span>
      </div>
    </div>
  );
}

function windowMinLabel(windowStart: number, windowEnd: number): string {
  const minutes = Math.max(1, Math.round((windowEnd - windowStart) / 60_000));
  return `${minutes} min`;
}

function LegendDot({
  color,
  label,
  shape = "circle",
}: {
  color: string;
  label: string;
  shape?: "circle" | "triangle";
}) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
      {shape === "triangle" ? (
        <span
          style={{
            width: 0,
            height: 0,
            borderLeft: "5px solid transparent",
            borderRight: "5px solid transparent",
            borderBottom: `9px solid ${color}`,
            display: "inline-block",
          }}
        />
      ) : (
        <span
          style={{
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: color,
            display: "inline-block",
          }}
        />
      )}
      {label}
    </span>
  );
}

function TableA({
  data,
  selectedId,
  onPick,
  limit,
  onShowMore,
}: {
  data: MockReq[];
  selectedId?: string;
  onPick: (r: MockReq) => void;
  limit: number;
  onShowMore: () => void;
}) {
  const visible = data.slice(0, limit);
  const remaining = Math.max(0, data.length - limit);
  return (
    <div
      style={{
        maxHeight: 320,
        overflowY: "auto",
        border: "1px solid var(--border-weak)",
        borderRadius: 6,
        position: "relative",
        zIndex: 0,
      }}
    >
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
        <thead
          style={{
            position: "sticky",
            top: 0,
            background: "rgba(0,0,0,0.6)",
            backdropFilter: "blur(8px)",
            zIndex: 1,
          }}
        >
          <tr style={{ color: "var(--text-muted)", textAlign: "left" }}>
            <Th>Time</Th>
            <Th>Method</Th>
            <Th>Path</Th>
            <Th>Caller</Th>
            <Th align="right">Status</Th>
            <Th align="right">Duration</Th>
            <Th align="right">Size</Th>
            <Th></Th>
          </tr>
        </thead>
        <tbody>
          {visible.length === 0 && (
            <tr>
              <td
                colSpan={8}
                style={{
                  padding: "18px 14px",
                  textAlign: "center",
                  color: "var(--text-muted)",
                  fontSize: 11,
                }}
              >
                No requests match the current filter.
              </td>
            </tr>
          )}
          {visible.map((d) => (
            <tr
              key={d.id}
              tabIndex={0}
              onClick={() => onPick(d)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onPick(d);
                }
              }}
              style={{
                cursor: "pointer",
                background: selectedId === d.id ? "rgba(122,167,255,0.08)" : undefined,
                borderTop: "1px solid var(--border-weak)",
                outline: "none",
              }}
            >
              <Td>{fmtTime(d.ts)}</Td>
              <Td>
                <MethodPill method={d.method} />
              </Td>
              <Td>
                <code style={{ fontSize: 11 }}>{d.path}</code>
              </Td>
              <Td>
                <span style={{ color: "var(--text-muted)" }}>
                  {d.caller.split("@")[0]}
                </span>
              </Td>
              <Td align="right">
                <span
                  style={{
                    display: "inline-flex",
                    justifyContent: "flex-end",
                    gap: 4,
                    flexWrap: "wrap",
                  }}
                >
                  <StatusPill code={d.status} />
                  {d.degraded && <DegradedPill />}
                </span>
              </Td>
              <Td align="right">
                <span
                  style={{
                    color: latencyTone(d.durationMs),
                    fontVariantNumeric: "tabular-nums",
                  }}
                >
                  {fmtMs(d.durationMs)}
                </span>
              </Td>
              <Td align="right">
                <span style={{ color: "var(--text-muted)" }}>
                  {fmtBytes(d.responseBytes)}
                </span>
              </Td>
              <Td align="right">
                <ChevronRight
                  size={11}
                  style={{ color: "var(--text-faint, var(--text-muted))" }}
                />
              </Td>
            </tr>
          ))}
        </tbody>
      </table>
      {remaining > 0 && (
        <button
          type="button"
          onClick={onShowMore}
          style={{
            width: "100%",
            padding: "8px 10px",
            background: "rgba(255,255,255,0.04)",
            border: "none",
            borderTop: "1px solid var(--border-weak)",
            color: "var(--text-muted)",
            cursor: "pointer",
            fontSize: 11,
            fontFamily: "inherit",
          }}
        >
          Show {Math.min(50, remaining)} more · {remaining} hidden
        </button>
      )}
    </div>
  );
}

function Th({ children, align }: { children?: React.ReactNode; align?: "right" }) {
  return (
    <th
      style={{
        padding: "6px 10px",
        fontSize: 10,
        fontWeight: 500,
        textTransform: "uppercase",
        letterSpacing: "0.05em",
        textAlign: align ?? "left",
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </th>
  );
}
function Td({ children, align }: { children?: React.ReactNode; align?: "right" }) {
  return (
    <td
      style={{
        padding: "6px 10px",
        whiteSpace: "nowrap",
        textAlign: align ?? "left",
        color: "var(--text-primary)",
      }}
    >
      {children}
    </td>
  );
}

function InlineRequestDetail({ req, onClose }: { req: MockReq; onClose: () => void }) {
  const curl = useMemo(() => buildCurl(req), [req]);
  return (
    <div
      role="region"
      aria-label={`Selected request detail: ${req.method} ${req.path}`}
      style={{
        marginTop: 12,
        padding: 12,
        border: "1px solid rgba(122,167,255,0.32)",
        borderRadius: 8,
        background:
          "linear-gradient(180deg, rgba(122,167,255,0.10), rgba(255,255,255,0.045))",
        boxShadow: "0 10px 28px rgba(0,0,0,0.24)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 10,
          marginBottom: 10,
        }}
      >
        <div style={{ minWidth: 0 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              marginBottom: 4,
              flexWrap: "wrap",
            }}
          >
            <MethodPill method={req.method} />
            <StatusPill code={req.status} />
            {req.degraded && <DegradedPill />}
            <span
              style={{
                color: latencyTone(req.durationMs),
                fontSize: 11,
                fontWeight: 700,
                fontVariantNumeric: "tabular-nums",
              }}
            >
              {fmtMs(req.durationMs)}
            </span>
            <span style={{ color: "var(--text-faint)", fontSize: 10 }}>
              {fmtTime(req.ts)}
            </span>
          </div>
          <code
            style={{
              display: "block",
              color: "var(--text-primary)",
              fontSize: 12,
              lineHeight: 1.4,
              whiteSpace: "normal",
              wordBreak: "break-all",
            }}
          >
            {req.path}
          </code>
        </div>
        <div style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
          <CopyActionButton
            value={curl}
            label="curl"
            title="Copy as curl (Authorization redacted)"
            iconSize={11}
            style={{ padding: "3px 7px" }}
          />
          <button
            type="button"
            className="glass-button"
            onClick={onClose}
            aria-label="Close selected request detail"
            title="Close detail"
            style={{
              padding: 4,
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <X size={12} />
          </button>
        </div>
      </div>
      <DetailContent r={req} />
    </div>
  );
}

function Drawer({ req, onClose }: { req: MockReq; onClose: () => void }) {
  const curl = useMemo(() => buildCurl(req), [req]);
  return (
    <div
      role="dialog"
      aria-label={`Request detail: ${req.method} ${req.path}`}
      style={{
        position: "absolute",
        top: 0,
        right: 0,
        bottom: 0,
        width: 380,
        background: "rgba(8, 12, 24, 0.95)",
        borderLeft: "1px solid var(--border-weak)",
        padding: 14,
        overflowY: "auto",
        zIndex: 20,
        boxShadow: "-12px 0 32px rgba(0,0,0,0.45)",
        animation: "slideIn 180ms ease-out",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 10,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12 }}>
          <MethodPill method={req.method} />
          <StatusPill code={req.status} />
          {req.degraded && <DegradedPill />}
          <code style={{ fontSize: 11 }}>{req.path}</code>
        </div>
        <div style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
          <CopyActionButton
            value={curl}
            label="curl"
            title="Copy as curl (Authorization redacted)"
            iconSize={11}
            style={{ padding: "3px 7px" }}
          />
          <button
            type="button"
            className="glass-button"
            onClick={onClose}
            aria-label="Close request detail (Esc)"
            title="Close (Esc)"
            style={{
              padding: 4,
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <X size={12} />
          </button>
        </div>
      </div>
      <DetailContent r={req} />
    </div>
  );
}

function buildCurl(r: MockReq): string {
  const parts: string[] = [`curl -X ${r.method} 'https://elb.example.com${r.path}'`];
  for (const [k, v] of Object.entries(r.requestHeaders)) {
    // Header values are already redacted in the fixture; in production the
    // backend redacts before serving so the copied curl is always safe.
    parts.push(`  -H '${k}: ${v}'`);
  }
  if (r.requestBody) {
    const body = r.requestBody.replace(/'/g, "'\\''");
    parts.push(`  --data '${body}'`);
  }
  return parts.join(" \\\n");
}

/* ==================================================================== */
/* VARIANT B — Live lane stream + centered modal                        */
/* ==================================================================== */

function Header({
  eyebrow,
  title,
  blurb,
  right,
}: {
  eyebrow: string;
  title: string;
  blurb?: string;
  right?: React.ReactNode;
}) {
  return (
    <div
      style={{
        display: "flex",
        justifyContent: "space-between",
        alignItems: "flex-start",
        gap: 12,
        marginBottom: 4,
      }}
    >
      <div>
        <div
          style={{
            fontSize: 10,
            textTransform: "uppercase",
            letterSpacing: "0.08em",
            color: "var(--accent)",
            fontWeight: 600,
            marginBottom: 2,
          }}
        >
          {eyebrow}
        </div>
        <h3 style={{ margin: 0, fontSize: 15, fontWeight: 600 }}>{title}</h3>
        {blurb && (
          <p
            style={{
              margin: "2px 0 0",
              fontSize: 11,
              color: "var(--text-muted)",
              maxWidth: 720,
              lineHeight: 1.5,
            }}
          >
            {blurb}
          </p>
        )}
      </div>
      {right}
    </div>
  );
}
