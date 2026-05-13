import { useState, useMemo, useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Loader2,
  ChevronDown,
  Play,
  Copy,
  Check,
  ExternalLink,
  Server,
  Shield,
  Briefcase,
  AlertTriangle,
  RefreshCw,
  Zap,
  Clock,
  Hash,
  BookOpen,
  CircleDot,
  Power,
  Package,
  Rocket,
} from "lucide-react";
import { Link } from "react-router-dom";

import { monitoringApi, aksApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import { loadSavedConfig } from "@/components/SetupWizard";

// ---------------------------------------------------------------------------
// Types (parsed from openapi.json)
// ---------------------------------------------------------------------------
interface SpecParam {
  name: string;
  in: string;
  required?: boolean;
  description?: string;
  schema?: { type?: string; default?: unknown };
}

interface SpecEndpoint {
  method: string;
  path: string;
  summary?: string;
  description?: string;
  tags: string[];
  parameters: SpecParam[];
  requestBody?: {
    required?: boolean;
    content?: Record<
      string,
      {
        schema?: Record<string, unknown>;
        examples?: Record<
          string,
          { summary?: string; description?: string; value: unknown }
        >;
      }
    >;
  };
  responses?: Record<string, { description?: string }>;
}

interface ParsedSpec {
  title: string;
  version: string;
  description: string;
  tags: { name: string; description?: string }[];
  endpoints: SpecEndpoint[];
  baseUrl: string;
}

const SVC_NAME = "elb-openapi";

const METHOD_META: Record<string, { color: string; bg: string; glow: string }> = {
  get: { color: "#6e9fff", bg: "rgba(110,159,255,0.10)", glow: "rgba(110,159,255,0.25)" },
  post: {
    color: "#73bf69",
    bg: "rgba(115,191,105,0.10)",
    glow: "rgba(115,191,105,0.25)",
  },
  delete: {
    color: "#f2726f",
    bg: "rgba(242,114,111,0.10)",
    glow: "rgba(242,114,111,0.25)",
  },
  put: { color: "#f2994a", bg: "rgba(242,153,74,0.10)", glow: "rgba(242,153,74,0.25)" },
  patch: { color: "#f2994a", bg: "rgba(242,153,74,0.10)", glow: "rgba(242,153,74,0.25)" },
};

const TAG_ICONS: Record<string, typeof Server> = {
  System: Shield,
  Cluster: Server,
  Jobs: Briefcase,
};

// ---------------------------------------------------------------------------
// Parse openapi.json → structured data
// ---------------------------------------------------------------------------
function parseSpec(raw: Record<string, unknown>, baseUrl: string): ParsedSpec {
  const info = (raw.info || {}) as Record<string, string>;
  const tags = (raw.tags || []) as { name: string; description?: string }[];
  const paths = (raw.paths || {}) as Record<
    string,
    Record<string, Record<string, unknown>>
  >;
  const endpoints: SpecEndpoint[] = [];

  for (const [path, methods] of Object.entries(paths)) {
    for (const [method, detail] of Object.entries(methods)) {
      if (!["get", "post", "put", "delete", "patch"].includes(method)) continue;
      endpoints.push({
        method,
        path,
        summary: detail.summary as string | undefined,
        description: detail.description as string | undefined,
        tags: (detail.tags as string[]) || [],
        parameters: (detail.parameters as SpecParam[]) || [],
        requestBody: detail.requestBody as SpecEndpoint["requestBody"],
        responses: detail.responses as SpecEndpoint["responses"],
      });
    }
  }

  return {
    title: info.title || "API",
    version: info.version || "",
    description: info.description || "",
    tags,
    endpoints,
    baseUrl,
  };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** True when endpoint can be executed without any user input */
function isSimpleEndpoint(ep: SpecEndpoint): boolean {
  const hasRequiredPathParams = ep.parameters.some((p) => p.in === "path" && p.required);
  return ep.method === "get" && !hasRequiredPathParams && !ep.requestBody;
}

function statusColor(code: number): string {
  if (code >= 200 && code < 300) return "var(--success)";
  if (code >= 400 && code < 500) return "var(--warning)";
  if (code >= 500) return "var(--danger)";
  return "var(--text-muted)";
}

// ---------------------------------------------------------------------------
// MethodBadge
// ---------------------------------------------------------------------------
function MethodBadge({ method, size = "md" }: { method: string; size?: "sm" | "md" }) {
  const m = METHOD_META[method] || METHOD_META.get;
  const px = size === "sm" ? "6px 8px" : "4px 10px";
  const fs = size === "sm" ? 9 : 10;
  return (
    <span
      style={{
        padding: px,
        borderRadius: 4,
        fontSize: fs,
        fontWeight: 700,
        textTransform: "uppercase",
        letterSpacing: "0.06em",
        color: m.color,
        background: m.bg,
        border: `1px solid ${m.glow}`,
        minWidth: size === "sm" ? 40 : 54,
        textAlign: "center",
        display: "inline-block",
        lineHeight: 1.3,
        fontFamily: "var(--font-mono)",
      }}
    >
      {method}
    </span>
  );
}

// ---------------------------------------------------------------------------
// JsonHighlight — lightweight JSON syntax coloring
// ---------------------------------------------------------------------------
function JsonHighlight({ text }: { text: string }) {
  // Muted, readable palette — not too flashy
  const S: Record<string, React.CSSProperties> = {
    key: { color: "#8cb4ff" }, // soft blue
    str: { color: "#a8d4a2" }, // sage green
    num: { color: "#d4b88c" }, // warm sand
    bool: { color: "#c9a0dc" }, // soft purple
    nil: { color: "#9da5b4", fontStyle: "italic" }, // muted grey
    brace: { color: "#7a8194" }, // dim bracket
  };

  const parts: React.ReactNode[] = [];
  // Regex tokeniser: strings (with key detection), numbers, bools, null, brackets
  const re =
    /("(?:[^"\\]|\\.)*")\s*(:?)|(\b(?:true|false)\b)|(\bnull\b)|([\d](?:[\d.eE+\-])*)|([{}[\],])/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text)) !== null) {
    // plain text before this match
    if (m.index > last) parts.push(text.slice(last, m.index));
    if (m[1]) {
      // string or key
      if (m[2]) {
        // key
        parts.push(
          <span key={i} style={S.key}>
            {m[1]}
          </span>,
        );
        parts.push(
          <span key={i + "c"} style={S.brace}>
            {m[2]}
          </span>,
        );
      } else {
        parts.push(
          <span key={i} style={S.str}>
            {m[1]}
          </span>,
        );
      }
    } else if (m[3]) {
      parts.push(
        <span key={i} style={S.bool}>
          {m[3]}
        </span>,
      );
    } else if (m[4]) {
      parts.push(
        <span key={i} style={S.nil}>
          {m[4]}
        </span>,
      );
    } else if (m[5]) {
      parts.push(
        <span key={i} style={S.num}>
          {m[5]}
        </span>,
      );
    } else if (m[6]) {
      parts.push(
        <span key={i} style={S.brace}>
          {m[6]}
        </span>,
      );
    }
    last = m.index + m[0].length;
    i++;
  }
  if (last < text.length) parts.push(text.slice(last));
  return <>{parts}</>;
}

// ---------------------------------------------------------------------------
// ResponseViewer — premium response display
// ---------------------------------------------------------------------------
function ResponseViewer({
  response,
  onCopy,
}: {
  response: { status: number; body: string; time: number };
  onCopy: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const isOk = response.status >= 200 && response.status < 300;
  const borderColor = isOk ? "rgba(115,191,105,0.25)" : "rgba(242,114,111,0.25)";

  const doCopy = () => {
    onCopy();
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div
      style={{
        borderRadius: 8,
        overflow: "hidden",
        border: `1px solid ${borderColor}`,
        background: "var(--bg-secondary)",
      }}
    >
      {/* Status bar */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "8px 14px",
          background: isOk ? "rgba(115,191,105,0.04)" : "rgba(242,114,111,0.04)",
          borderBottom: `1px solid ${borderColor}`,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <CircleDot size={10} style={{ color: statusColor(response.status) }} />
          <span
            style={{
              fontSize: 13,
              fontWeight: 700,
              fontFamily: "var(--font-mono)",
              color: statusColor(response.status),
            }}
          >
            {response.status || "Error"}
          </span>
          <span
            style={{
              fontSize: 11,
              color: "var(--text-faint)",
              display: "flex",
              alignItems: "center",
              gap: 3,
            }}
          >
            <Clock size={10} /> {response.time}ms
          </span>
        </div>
        <button
          className="glass-button"
          onClick={doCopy}
          style={{ padding: "3px 8px", fontSize: 10 }}
        >
          {copied ? (
            <>
              <Check size={10} /> Copied
            </>
          ) : (
            <>
              <Copy size={10} /> Copy
            </>
          )}
        </button>
      </div>
      {/* Body */}
      <pre
        style={{
          margin: 0,
          padding: "12px 14px",
          fontSize: 11,
          lineHeight: 1.6,
          maxHeight: 400,
          overflow: "auto",
          color: "var(--text-primary)",
          fontFamily: "var(--font-mono)",
          background: "transparent",
        }}
      >
        <JsonHighlight text={response.body} />
      </pre>
    </div>
  );
}

// ---------------------------------------------------------------------------
// SectionLabel
// ---------------------------------------------------------------------------
function SectionLabel({
  children,
  style,
}: {
  children: React.ReactNode;
  style?: React.CSSProperties;
}) {
  return (
    <div
      style={{
        fontSize: 10,
        textTransform: "uppercase",
        letterSpacing: "0.08em",
        color: "var(--text-faint)",
        fontWeight: 700,
        marginBottom: 8,
        ...style,
      }}
    >
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// EndpointCard — premium endpoint card
// ---------------------------------------------------------------------------
function EndpointCard({
  ep,
  baseUrl,
  id,
}: {
  ep: SpecEndpoint;
  baseUrl: string;
  id: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [paramValues, setParamValues] = useState<Record<string, string>>({});
  const [bodyText, setBodyText] = useState("");
  const [selectedExample, setSelectedExample] = useState("");
  const [response, setResponse] = useState<{
    status: number;
    body: string;
    time: number;
  } | null>(null);
  const [loading, setLoading] = useState(false);

  const m = METHOD_META[ep.method] || METHOD_META.get;
  const examples = ep.requestBody?.content?.["application/json"]?.examples || {};
  const exampleKeys = Object.keys(examples);
  const simple = isSimpleEndpoint(ep);

  const initBody = useCallback(() => {
    if (exampleKeys.length > 0 && !bodyText) {
      const first = exampleKeys[0];
      setSelectedExample(first);
      setBodyText(JSON.stringify(examples[first].value, null, 2));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [exampleKeys.length]);

  const handleExampleChange = (key: string) => {
    setSelectedExample(key);
    const ex = examples[key];
    if (ex) setBodyText(JSON.stringify(ex.value, null, 2));
  };

  const handleExecute = useCallback(async () => {
    setLoading(true);
    setResponse(null);
    let url = baseUrl + ep.path;
    for (const p of ep.parameters.filter((p) => p.in === "path")) {
      url = url.replace(`{${p.name}}`, paramValues[p.name] || "");
    }
    const start = Date.now();
    try {
      const opts: RequestInit = {
        method: ep.method.toUpperCase(),
        headers: { "Content-Type": "application/json" },
      };
      if (ep.requestBody && bodyText) opts.body = bodyText;
      const resp = await fetch(url, opts);
      const text = await resp.text();
      let formatted = text;
      try {
        formatted = JSON.stringify(JSON.parse(text), null, 2);
      } catch {
        /* not json */
      }
      setResponse({ status: resp.status, body: formatted, time: Date.now() - start });
    } catch (e) {
      setResponse({ status: 0, body: String(e), time: Date.now() - start });
    } finally {
      setLoading(false);
    }
  }, [baseUrl, ep, paramValues, bodyText]);

  /** Try it: simple GET → expand + execute immediately. Others → expand for input. */
  const handleTryIt = () => {
    if (!expanded) {
      setExpanded(true);
      initBody();
    }
    if (simple) {
      handleExecute();
    }
  };

  const handleCopy = () => {
    if (response) navigator.clipboard.writeText(response.body).catch(() => {});
  };

  return (
    <div
      id={id}
      style={{
        background: "var(--bg-primary)",
        border: `1px solid var(--border-weak)`,
        borderRadius: 10,
        overflow: "hidden",
        transition: "all var(--motion-base)",
        boxShadow: expanded
          ? `0 0 0 1px ${m.glow}, var(--shadow-panel)`
          : "var(--shadow-panel)",
      }}
    >
      {/* Header row */}
      <div
        onClick={() => {
          setExpanded((e) => !e);
          if (!expanded) initBody();
        }}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          width: "100%",
          padding: "12px 16px",
          cursor: "pointer",
          borderLeft: `3px solid ${expanded ? m.color : "transparent"}`,
          transition: "border-color var(--motion-base)",
        }}
      >
        <MethodBadge method={ep.method} />
        <code
          style={{
            fontSize: 13,
            color: "var(--text-primary)",
            flex: 1,
            fontFamily: "var(--font-mono)",
            fontWeight: 500,
          }}
        >
          {ep.path}
        </code>
        <span
          style={{
            fontSize: 12,
            color: "var(--text-faint)",
            maxWidth: 300,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {ep.summary}
        </span>

        {/* Inline Try-it for simple GETs */}
        {simple && !expanded && (
          <button
            className="glass-button glass-button--primary"
            onClick={(e) => {
              e.stopPropagation();
              handleTryIt();
            }}
            style={{ padding: "3px 10px", fontSize: 10, gap: 4 }}
          >
            <Zap size={10} /> Try
          </button>
        )}

        <ChevronDown
          size={14}
          style={{
            transform: expanded ? "rotate(0)" : "rotate(-90deg)",
            transition: "transform var(--motion-fast)",
            color: "var(--text-faint)",
            flexShrink: 0,
          }}
        />
      </div>

      {/* Expanded body */}
      {expanded && (
        <div
          style={{
            borderTop: `1px solid var(--border-weak)`,
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: 0,
          }}
        >
          {/* Left: Documentation */}
          <div
            style={{ padding: "16px 20px", borderRight: "1px solid var(--border-weak)" }}
          >
            {ep.description && (
              <p
                style={{
                  fontSize: 12,
                  color: "var(--text-muted)",
                  margin: "0 0 16px",
                  lineHeight: 1.7,
                  whiteSpace: "pre-wrap",
                }}
              >
                {ep.description}
              </p>
            )}

            {/* Parameters */}
            {ep.parameters.length > 0 && (
              <div style={{ marginBottom: 16 }}>
                <SectionLabel>Parameters</SectionLabel>
                <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
                  {ep.parameters.map((p) => (
                    <div
                      key={p.name}
                      style={{
                        display: "grid",
                        gridTemplateColumns: "120px 60px 1fr",
                        gap: 8,
                        padding: "8px 0",
                        borderBottom: "1px solid var(--border-weak)",
                        alignItems: "baseline",
                      }}
                    >
                      <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                        <code
                          style={{
                            color: m.color,
                            fontSize: 12,
                            fontFamily: "var(--font-mono)",
                          }}
                        >
                          {p.name}
                        </code>
                        {p.required && (
                          <span
                            style={{
                              fontSize: 8,
                              fontWeight: 700,
                              color: "var(--danger)",
                              textTransform: "uppercase",
                              letterSpacing: "0.05em",
                            }}
                          >
                            req
                          </span>
                        )}
                      </div>
                      <span
                        style={{
                          fontSize: 10,
                          color: "var(--text-faint)",
                          fontFamily: "var(--font-mono)",
                          background: "var(--bg-tertiary)",
                          padding: "1px 5px",
                          borderRadius: 3,
                          textAlign: "center",
                        }}
                      >
                        {p.schema?.type || "string"}
                      </span>
                      <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
                        {p.description || ""}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Responses */}
            {ep.responses && (
              <div>
                <SectionLabel>Responses</SectionLabel>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {Object.entries(ep.responses).map(([code, info]) => (
                    <span
                      key={code}
                      style={{
                        fontSize: 11,
                        padding: "3px 10px",
                        borderRadius: 5,
                        fontFamily: "var(--font-mono)",
                        fontWeight: 600,
                        background: code.startsWith("2")
                          ? "rgba(115,191,105,0.08)"
                          : code.startsWith("4")
                            ? "rgba(242,114,111,0.08)"
                            : "var(--bg-tertiary)",
                        color: code.startsWith("2")
                          ? "var(--success)"
                          : code.startsWith("4")
                            ? "var(--danger)"
                            : "var(--text-muted)",
                        border: `1px solid ${
                          code.startsWith("2")
                            ? "rgba(115,191,105,0.15)"
                            : code.startsWith("4")
                              ? "rgba(242,114,111,0.15)"
                              : "var(--border-weak)"
                        }`,
                      }}
                    >
                      {code}{" "}
                      <span style={{ fontWeight: 400, fontFamily: "inherit" }}>
                        {(info as { description?: string }).description}
                      </span>
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* Right: Try it panel */}
          <div
            style={{
              padding: "16px 20px",
              background: "var(--bg-secondary)",
              minHeight: 120,
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                marginBottom: 14,
              }}
            >
              <SectionLabel style={{ margin: 0 }}>Try it</SectionLabel>
              <button
                className="glass-button glass-button--primary"
                onClick={handleExecute}
                disabled={loading}
                style={{ fontSize: 11, gap: 5, padding: "5px 14px" }}
              >
                {loading ? (
                  <>
                    <Loader2 size={12} className="spin" /> Sending...
                  </>
                ) : (
                  <>
                    <Play size={12} /> Send Request
                  </>
                )}
              </button>
            </div>

            {/* Path param inputs */}
            {ep.parameters.filter((p) => p.in === "path").length > 0 && (
              <div style={{ marginBottom: 12 }}>
                <div
                  style={{
                    fontSize: 10,
                    color: "var(--text-faint)",
                    marginBottom: 6,
                    fontWeight: 600,
                    textTransform: "uppercase",
                  }}
                >
                  Path Parameters
                </div>
                {ep.parameters
                  .filter((p) => p.in === "path")
                  .map((p) => (
                    <div
                      key={p.name}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 8,
                        marginBottom: 6,
                      }}
                    >
                      <label
                        style={{
                          fontSize: 11,
                          color: "var(--text-muted)",
                          minWidth: 80,
                          fontFamily: "var(--font-mono)",
                        }}
                      >
                        {p.name}
                      </label>
                      <input
                        type="text"
                        placeholder={
                          p.schema?.default != null ? String(p.schema.default) : p.name
                        }
                        value={paramValues[p.name] || ""}
                        onChange={(e) =>
                          setParamValues((prev) => ({
                            ...prev,
                            [p.name]: e.target.value,
                          }))
                        }
                        style={{
                          flex: 1,
                          padding: "6px 10px",
                          fontSize: 12,
                          background: "var(--bg-primary)",
                          border: "1px solid var(--border-weak)",
                          borderRadius: 6,
                          color: "var(--text-primary)",
                          outline: "none",
                          fontFamily: "var(--font-mono)",
                          transition: "border-color var(--motion-fast)",
                        }}
                        onFocus={(e) => {
                          e.target.style.borderColor = "var(--border-focus)";
                        }}
                        onBlur={(e) => {
                          e.target.style.borderColor = "var(--border-weak)";
                        }}
                      />
                    </div>
                  ))}
              </div>
            )}

            {/* Request body */}
            {ep.requestBody && (
              <div style={{ marginBottom: 12 }}>
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    marginBottom: 6,
                  }}
                >
                  <span
                    style={{
                      fontSize: 10,
                      color: "var(--text-faint)",
                      fontWeight: 600,
                      textTransform: "uppercase",
                    }}
                  >
                    Request Body
                  </span>
                  {exampleKeys.length > 0 && (
                    <select
                      value={selectedExample}
                      onChange={(e) => handleExampleChange(e.target.value)}
                      style={{
                        background: "var(--bg-primary)",
                        color: "var(--text-primary)",
                        border: "1px solid var(--border-weak)",
                        borderRadius: 5,
                        fontSize: 10,
                        padding: "2px 8px",
                        fontFamily: "var(--font-mono)",
                      }}
                    >
                      {exampleKeys.map((k) => (
                        <option key={k} value={k}>
                          {examples[k].summary || k}
                        </option>
                      ))}
                    </select>
                  )}
                </div>
                {selectedExample && examples[selectedExample]?.description && (
                  <p
                    style={{
                      fontSize: 10,
                      color: "var(--text-faint)",
                      margin: "0 0 6px",
                      fontStyle: "italic",
                    }}
                  >
                    {examples[selectedExample].description}
                  </p>
                )}
                <textarea
                  value={bodyText}
                  onChange={(e) => setBodyText(e.target.value)}
                  rows={Math.min(14, Math.max(4, bodyText.split("\n").length + 1))}
                  style={{
                    width: "100%",
                    padding: "10px 12px",
                    fontSize: 11,
                    fontFamily: "var(--font-mono)",
                    background: "var(--bg-primary)",
                    border: "1px solid var(--border-weak)",
                    borderRadius: 8,
                    color: "var(--text-primary)",
                    resize: "vertical",
                    outline: "none",
                    lineHeight: 1.6,
                    transition: "border-color var(--motion-fast)",
                  }}
                  onFocus={(e) => {
                    e.target.style.borderColor = "var(--border-focus)";
                  }}
                  onBlur={(e) => {
                    e.target.style.borderColor = "var(--border-weak)";
                  }}
                />
              </div>
            )}

            {/* Response */}
            {response && <ResponseViewer response={response} onCopy={handleCopy} />}

            {/* Empty state for simple endpoints */}
            {!response && !loading && simple && (
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  justifyContent: "center",
                  padding: "24px 0",
                  color: "var(--text-faint)",
                  fontSize: 11,
                }}
              >
                <Zap size={20} style={{ opacity: 0.3, marginBottom: 6 }} />
                Click &ldquo;Send Request&rdquo; to execute
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// TagSection — group of endpoints with anchor
// ---------------------------------------------------------------------------
function TagSection({
  tag,
  endpoints,
  baseUrl,
}: {
  tag: { name: string; description?: string };
  endpoints: SpecEndpoint[];
  baseUrl: string;
}) {
  const [open, setOpen] = useState(true);
  const Icon = TAG_ICONS[tag.name] || Server;

  return (
    <section id={`tag-${tag.name}`}>
      <button
        onClick={() => setOpen((o) => !o)}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          width: "100%",
          background: "none",
          border: "none",
          cursor: "pointer",
          padding: "10px 0",
          color: "var(--text-primary)",
        }}
      >
        <div
          style={{
            width: 28,
            height: 28,
            borderRadius: 8,
            background: "var(--bg-tertiary)",
            display: "grid",
            placeItems: "center",
          }}
        >
          <Icon size={14} strokeWidth={1.5} style={{ color: "var(--accent)" }} />
        </div>
        <div style={{ flex: 1, textAlign: "left" }}>
          <span style={{ fontSize: 15, fontWeight: 700 }}>{tag.name}</span>
          {tag.description && (
            <span style={{ fontSize: 11, color: "var(--text-faint)", marginLeft: 8 }}>
              {tag.description}
            </span>
          )}
        </div>
        <span
          style={{
            fontSize: 10,
            color: "var(--text-faint)",
            background: "var(--bg-tertiary)",
            padding: "2px 8px",
            borderRadius: 10,
            fontFamily: "var(--font-mono)",
          }}
        >
          {endpoints.length}
        </span>
        <ChevronDown
          size={14}
          style={{
            color: "var(--text-faint)",
            transform: open ? "rotate(0)" : "rotate(-90deg)",
            transition: "transform var(--motion-fast)",
          }}
        />
      </button>
      {open && (
        <div
          style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 24 }}
        >
          {endpoints.map((ep) => (
            <EndpointCard
              key={`${ep.method}-${ep.path}`}
              ep={ep}
              baseUrl={baseUrl}
              id={`ep-${ep.method}-${ep.path.replace(/\//g, "-")}`}
            />
          ))}
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Hero header
// ---------------------------------------------------------------------------
function ApiHero({
  spec,
  baseUrl,
  onRefresh,
  refreshing,
}: {
  spec: ParsedSpec | null;
  baseUrl: string | null;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  const totalEndpoints = spec?.endpoints.length ?? 0;
  const methods = spec ? [...new Set(spec.endpoints.map((e) => e.method))] : [];

  return (
    <div
      style={{
        background: "var(--bg-primary)",
        border: "1px solid var(--border-weak)",
        borderRadius: 12,
        padding: "28px 32px",
        position: "relative",
        overflow: "hidden",
      }}
    >
      {/* Subtle gradient accent */}
      <div
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          right: 0,
          height: 3,
          background:
            "linear-gradient(90deg, var(--accent) 0%, var(--purple) 50%, var(--teal) 100%)",
          opacity: 0.6,
        }}
      />

      <div
        style={{
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "space-between",
          gap: 16,
        }}
      >
        <div>
          <div
            style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}
          >
            <BookOpen size={18} style={{ color: "var(--accent)" }} />
            <h1
              style={{
                margin: 0,
                fontSize: 22,
                fontWeight: 700,
                letterSpacing: "-0.02em",
                color: "var(--text-primary)",
              }}
            >
              API Reference
            </h1>
            {spec && (
              <span
                style={{
                  fontSize: 10,
                  padding: "2px 8px",
                  borderRadius: 10,
                  background: "var(--bg-tertiary)",
                  color: "var(--text-faint)",
                  fontFamily: "var(--font-mono)",
                  fontWeight: 600,
                }}
              >
                v{spec.version}
              </span>
            )}
          </div>
          <p
            style={{
              margin: 0,
              fontSize: 13,
              color: "var(--text-muted)",
              lineHeight: 1.5,
            }}
          >
            {spec
              ? spec.description.split("\n")[0]
              : "ElasticBLAST REST API Documentation"}
          </p>

          {/* Stats row */}
          {spec && (
            <div
              style={{
                display: "flex",
                gap: 16,
                marginTop: 14,
              }}
            >
              <Stat icon={<Hash size={11} />} label="Endpoints" value={totalEndpoints} />
              <Stat icon={<Server size={11} />} label="Groups" value={spec.tags.length} />
              <Stat
                icon={<Zap size={11} />}
                label="Methods"
                value={methods.map((m) => m.toUpperCase()).join(", ")}
              />
            </div>
          )}
        </div>

        {/* Actions */}
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexShrink: 0 }}>
          {baseUrl && (
            <>
              <span
                style={{
                  fontSize: 10,
                  fontFamily: "var(--font-mono)",
                  color: "var(--text-faint)",
                  padding: "3px 8px",
                  background: "var(--bg-tertiary)",
                  borderRadius: 5,
                }}
              >
                {baseUrl}
              </span>
              <a
                href={`${baseUrl}/docs`}
                target="_blank"
                rel="noreferrer"
                className="glass-button"
                style={{ fontSize: 11, textDecoration: "none" }}
              >
                <ExternalLink size={11} /> Swagger UI
              </a>
              <button
                className="glass-button"
                onClick={onRefresh}
                disabled={refreshing}
                style={{ fontSize: 11 }}
              >
                <RefreshCw size={11} className={refreshing ? "spin" : ""} />
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function Stat({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode;
  label: string;
  value: React.ReactNode;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 6,
        padding: "5px 12px",
        borderRadius: 8,
        background: "var(--bg-secondary)",
        border: "1px solid var(--border-weak)",
      }}
    >
      <span style={{ color: "var(--accent)", display: "flex" }}>{icon}</span>
      <span style={{ fontSize: 10, color: "var(--text-faint)" }}>{label}</span>
      <span
        style={{
          fontSize: 12,
          fontWeight: 600,
          color: "var(--text-primary)",
          fontFamily: "var(--font-mono)",
        }}
      >
        {value}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------
export function ApiReference() {
  const [savedConfig] = useState(() => loadSavedConfig());

  const sub = savedConfig?.subscriptionId ?? "";
  const rg = savedConfig?.workloadResourceGroup ?? "";
  const enabled = Boolean(sub && rg);

  // 1. Discover clusters
  const clustersQuery = useQuery({
    queryKey: ["aks", sub, rg],
    queryFn: () => monitoringApi.aks(sub, rg),
    enabled,
    staleTime: 300_000,
  });
  const clusterName = clustersQuery.data?.clusters?.[0]?.name ?? "";
  const clusters = clustersQuery.data?.clusters ?? [];
  const firstCluster = clusters[0];
  const clusterStopped = firstCluster && firstCluster.power_state !== "Running";

  // Check ACR for elb-openapi image
  const acrRg = savedConfig?.acrResourceGroup ?? "";
  const acrName = savedConfig?.acrName ?? "";
  const acrQuery = useQuery({
    queryKey: ["acr", sub, acrRg, acrName],
    queryFn: () => monitoringApi.acr(sub, acrRg, acrName),
    enabled: Boolean(sub && acrRg && acrName),
    staleTime: 300_000,
  });
  const hasOpenApiImage = acrQuery.data?.actual_tags
    ? "elb-openapi" in acrQuery.data.actual_tags
    : false;

  // 2. Discover service IP
  const svcQuery = useQuery({
    queryKey: ["openapi-svc", sub, rg, clusterName],
    queryFn: () => monitoringApi.serviceIp(sub, rg, clusterName, SVC_NAME),
    enabled: enabled && Boolean(clusterName),
    staleTime: 300_000,
    retry: 1,
  });
  const baseUrl = svcQuery.data ? `http://${svcQuery.data.external_ip}` : null;

  // 3. Fetch openapi.json dynamically
  const specQuery = useQuery({
    queryKey: ["openapi-spec", baseUrl],
    queryFn: async () => {
      const resp = await fetch(`${baseUrl}/openapi.json`);
      if (!resp.ok) throw new Error(`Failed: ${resp.status}`);
      return resp.json();
    },
    enabled: Boolean(baseUrl),
    staleTime: 60_000,
  });

  const spec = useMemo(() => {
    if (!specQuery.data || !baseUrl) return null;
    return parseSpec(specQuery.data, baseUrl);
  }, [specQuery.data, baseUrl]);

  // Group endpoints by tag
  const grouped = useMemo(() => {
    if (!spec) return [];
    return spec.tags
      .map((tag) => ({
        tag,
        endpoints: spec.endpoints.filter((ep) => ep.tags.includes(tag.name)),
      }))
      .filter((g) => g.endpoints.length > 0);
  }, [spec]);

  return (
    <div className="page-stack">
      {/* Hero */}
      <ApiHero
        spec={spec}
        baseUrl={baseUrl}
        onRefresh={() => specQuery.refetch()}
        refreshing={specQuery.isFetching}
      />

      {/* Loading / Error states */}
      {(!enabled || svcQuery.isLoading || clustersQuery.isLoading) &&
        enabled &&
        !clusterStopped && (
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              padding: "48px 0",
              gap: 12,
            }}
          >
            <Loader2 size={24} className="spin" style={{ color: "var(--accent)" }} />
            <p style={{ color: "var(--text-faint)", fontSize: 12 }}>
              Discovering OpenAPI service on AKS...
            </p>
          </div>
        )}

      {!enabled && (
        <div
          style={{
            background: "var(--bg-primary)",
            border: "1px solid var(--border-weak)",
            borderRadius: 10,
            textAlign: "center",
            padding: "40px 24px",
          }}
        >
          <AlertTriangle size={20} style={{ color: "var(--warning)", marginBottom: 8 }} />
          <p style={{ color: "var(--text-muted)", fontSize: 13 }}>
            Configure Subscription and Workload RG in the Dashboard first.
          </p>
        </div>
      )}

      {/* Smart diagnostics: AKS stopped */}
      {enabled && clusterStopped && (
        <div
          style={{
            background: "var(--bg-primary)",
            border: "1px solid rgba(242,153,74,0.2)",
            borderRadius: 10,
            padding: "24px 28px",
          }}
        >
          <div
            style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}
          >
            <div
              style={{
                width: 36,
                height: 36,
                borderRadius: 10,
                background: "rgba(242,153,74,0.1)",
                display: "grid",
                placeItems: "center",
              }}
            >
              <Power size={18} style={{ color: "var(--warning)" }} />
            </div>
            <div>
              <div style={{ fontWeight: 600, fontSize: 14 }}>AKS cluster is stopped</div>
              <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
                The OpenAPI service runs inside the AKS cluster. Start the cluster to
                access the API.
              </div>
            </div>
          </div>
          <div
            style={{
              display: "flex",
              gap: 8,
              flexWrap: "wrap",
              padding: "12px 16px",
              background: "var(--bg-secondary)",
              borderRadius: 8,
              fontSize: 12,
              color: "var(--text-muted)",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <Server size={12} style={{ color: "var(--text-faint)" }} />
              <span>{firstCluster?.name}</span>
            </div>
            <span style={{ color: "var(--border-medium)" }}>·</span>
            <span style={{ color: "var(--warning)", fontWeight: 600 }}>
              {firstCluster?.power_state}
            </span>
            <span style={{ color: "var(--border-medium)" }}>·</span>
            <span>{firstCluster?.region}</span>
          </div>
          <div style={{ marginTop: 12, display: "flex", gap: 8, alignItems: "center" }}>
            <Link
              to="/"
              className="glass-button glass-button--primary"
              style={{ fontSize: 12, textDecoration: "none" }}
            >
              <Power size={12} /> Go to Dashboard to start cluster
            </Link>
            <button
              className="glass-button"
              onClick={() => clustersQuery.refetch()}
              disabled={clustersQuery.isFetching}
              style={{ fontSize: 12 }}
            >
              <RefreshCw size={12} className={clustersQuery.isFetching ? "spin" : ""} />{" "}
              Refresh
            </button>
          </div>
        </div>
      )}

      {/* Smart diagnostics: OpenAPI image not built */}
      {enabled && !clusterStopped && acrQuery.isSuccess && !hasOpenApiImage && (
        <div
          style={{
            background: "var(--bg-primary)",
            border: "1px solid rgba(184,119,217,0.2)",
            borderRadius: 10,
            padding: "24px 28px",
          }}
        >
          <div
            style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}
          >
            <div
              style={{
                width: 36,
                height: 36,
                borderRadius: 10,
                background: "rgba(184,119,217,0.1)",
                display: "grid",
                placeItems: "center",
              }}
            >
              <Package size={18} style={{ color: "var(--purple)" }} />
            </div>
            <div>
              <div style={{ fontWeight: 600, fontSize: 14 }}>OpenAPI image not built</div>
              <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
                The{" "}
                <code
                  style={{
                    fontFamily: "var(--font-mono)",
                    background: "var(--bg-tertiary)",
                    padding: "1px 5px",
                    borderRadius: 3,
                  }}
                >
                  elb-openapi
                </code>{" "}
                container image needs to be built in your ACR before deploying the API
                service.
              </div>
            </div>
          </div>
          <Link
            to="/"
            className="glass-button glass-button--primary"
            style={{ fontSize: 12, textDecoration: "none" }}
          >
            <Package size={12} /> Build images from Dashboard ACR card
          </Link>
        </div>
      )}

      {/* Service not found — cluster running but service not deployed */}
      {svcQuery.isError && !clusterStopped && (
        <OpenApiDeployPanel
          subscriptionId={sub}
          resourceGroup={rg}
          clusterName={clusterName}
          acrName={acrName}
          storageAccount={savedConfig?.storageAccountName ?? ""}
          imageBuilt={hasOpenApiImage}
          onRetry={() => svcQuery.refetch()}
          retrying={svcQuery.isFetching}
        />
      )}

      {baseUrl && specQuery.isLoading && (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            padding: "32px 0",
            gap: 8,
          }}
        >
          <Loader2 size={20} className="spin" style={{ color: "var(--accent)" }} />
          <p style={{ color: "var(--text-faint)", fontSize: 12 }}>
            Loading API specification...
          </p>
        </div>
      )}

      {specQuery.isError && (
        <div
          style={{
            background: "var(--bg-primary)",
            border: "1px solid rgba(242,114,111,0.2)",
            borderRadius: 10,
            padding: "16px 20px",
          }}
        >
          <AlertTriangle
            size={14}
            style={{ color: "var(--danger)", verticalAlign: "middle", marginRight: 6 }}
          />
          <span style={{ fontSize: 12 }}>
            Failed to load openapi.json: {(specQuery.error as Error).message}
          </span>
        </div>
      )}

      {/* Endpoints */}
      {spec &&
        grouped.map(({ tag, endpoints }) => (
          <TagSection
            key={tag.name}
            tag={tag}
            endpoints={endpoints}
            baseUrl={spec.baseUrl}
          />
        ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// OpenAPI deploy panel — shown when AKS is up but the elb-openapi service
// has not been deployed yet (or was wiped). Lets the user trigger the
// deploy without going back to the Dashboard.
// ---------------------------------------------------------------------------
function OpenApiDeployPanel({
  subscriptionId,
  resourceGroup,
  clusterName,
  acrName,
  storageAccount,
  imageBuilt,
  onRetry,
  retrying,
}: {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
  acrName: string;
  storageAccount: string;
  imageBuilt: boolean;
  onRetry: () => void;
  retrying: boolean;
}) {
  const [deployState, setDeployState] = useState<
    "idle" | "deploying" | "waiting" | "error"
  >("idle");
  const [deployError, setDeployError] = useState<string | null>(null);
  const [waitElapsed, setWaitElapsed] = useState(0);

  const canDeploy =
    Boolean(subscriptionId && resourceGroup && clusterName && acrName) &&
    imageBuilt &&
    deployState !== "deploying" &&
    deployState !== "waiting";

  const handleDeploy = async () => {
    setDeployState("deploying");
    setDeployError(null);
    setWaitElapsed(0);
    try {
      await aksApi.deployOpenApi(
        subscriptionId,
        resourceGroup,
        clusterName,
        acrName,
        storageAccount,
      );
      setDeployState("waiting");
      // Start elapsed timer
      const start = Date.now();
      const timer = setInterval(() => {
        setWaitElapsed(Math.floor((Date.now() - start) / 1000));
      }, 1000);
      // Poll for the service every 10s for up to ~3 min
      const poll = () => {
        if (Date.now() - start > 180_000) {
          clearInterval(timer);
          return;
        }
        onRetry();
        setTimeout(poll, 10_000);
      };
      setTimeout(poll, 15_000);
      // Cleanup timer when component unmounts (handled by React lifecycle)
      return () => clearInterval(timer);
    } catch (err: unknown) {
      setDeployState("error");
      setDeployError(formatApiError(err));
    }
  };

  return (
    <div
      style={{
        background: "var(--bg-primary)",
        border: "1px solid rgba(242,153,74,0.2)",
        borderRadius: 10,
        padding: "20px 24px",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
        <AlertTriangle size={16} style={{ color: "var(--warning)" }} />
        <span style={{ fontWeight: 600, fontSize: 14 }}>OpenAPI service not found</span>
      </div>
      <p style={{ color: "var(--text-muted)", fontSize: 12, margin: "0 0 12px" }}>
        The{" "}
        <code
          style={{
            fontFamily: "var(--font-mono)",
            background: "var(--bg-tertiary)",
            padding: "1px 5px",
            borderRadius: 3,
          }}
        >
          elb-openapi
        </code>{" "}
        service is not running on <strong>{clusterName || "the cluster"}</strong>. Deploy
        it now to load the live API specification.
      </p>

      {!imageBuilt && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "8px 12px",
            marginBottom: 12,
            background: "rgba(184,119,217,0.08)",
            border: "1px solid rgba(184,119,217,0.2)",
            borderRadius: 6,
            fontSize: 11,
            color: "var(--text-muted)",
          }}
        >
          <Package size={12} style={{ color: "var(--purple)" }} />
          The <code style={{ fontFamily: "var(--font-mono)" }}>elb-openapi</code> image
          must be built first — open the ACR card on the Dashboard.
        </div>
      )}

      {deployState === "waiting" && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "10px 14px",
            marginBottom: 12,
            background: "rgba(122,167,255,0.06)",
            border: "1px solid rgba(122,167,255,0.2)",
            borderRadius: 6,
            fontSize: 12,
            color: "var(--accent)",
          }}
        >
          <Loader2 size={13} className="spin" />
          <span>
            Deployed — waiting for pod to start ({waitElapsed}s).
            {waitElapsed < 30 && " This usually takes 30–90 seconds."}
            {waitElapsed >= 30 && waitElapsed < 90 && " Almost there..."}
            {waitElapsed >= 90 &&
              " Taking longer than usual — the pod may be pulling the image."}
          </span>
        </div>
      )}

      {deployState === "error" && deployError && (
        <div
          style={{
            display: "flex",
            alignItems: "flex-start",
            gap: 8,
            padding: "8px 12px",
            marginBottom: 12,
            background: "rgba(242,114,111,0.08)",
            border: "1px solid rgba(242,114,111,0.2)",
            borderRadius: 6,
            fontSize: 11,
            color: "var(--danger)",
          }}
        >
          <AlertTriangle size={12} style={{ flexShrink: 0, marginTop: 1 }} />
          <span style={{ wordBreak: "break-word" }}>{deployError}</span>
        </div>
      )}

      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <button
          className="glass-button glass-button--primary"
          onClick={handleDeploy}
          disabled={!canDeploy}
          title={
            !imageBuilt
              ? "Build the elb-openapi image first"
              : !acrName
                ? "ACR is not configured"
                : "Deploy elb-openapi to AKS"
          }
          style={{ fontSize: 12 }}
        >
          {deployState === "deploying" ? (
            <>
              <Loader2 size={12} className="spin" /> Deploying...
            </>
          ) : deployState === "waiting" ? (
            <>
              <Loader2 size={12} className="spin" /> Waiting ({waitElapsed}s)
            </>
          ) : (
            <>
              <Rocket size={12} /> Deploy elb-openapi
            </>
          )}
        </button>
        <button
          className="glass-button"
          onClick={onRetry}
          disabled={retrying}
          style={{ fontSize: 12 }}
        >
          <RefreshCw size={12} className={retrying ? "spin" : ""} /> Retry Discovery
        </button>
      </div>
    </div>
  );
}
