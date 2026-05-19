import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, Link2, Loader2, Play, Zap } from "lucide-react";

import { METHOD_META } from "@/pages/apiReference/constants";
import { MethodBadge } from "@/pages/apiReference/MethodBadge";
import { ResponseViewer } from "@/pages/apiReference/ResponseViewer";
import { SectionLabel } from "@/pages/apiReference/SectionLabel";
import { isSimpleEndpoint } from "@/pages/apiReference/spec";
import type { OpenApiProxyInfo, SpecEndpoint } from "@/pages/apiReference/types";
import { useOpenApiExecutor } from "@/hooks/useOpenApiExecutor";

export function EndpointCard({
  ep,
  baseUrl,
  proxyInfo,
  id,
}: {
  ep: SpecEndpoint;
  baseUrl: string;
  proxyInfo?: OpenApiProxyInfo;
  id: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [paramValues, setParamValues] = useState<Record<string, string>>({});
  const [bodyText, setBodyText] = useState("");
  const [selectedExample, setSelectedExample] = useState("");
  const [copiedAnchor, setCopiedAnchor] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const { execute, response, loading, copyResponse } = useOpenApiExecutor({
    endpoint: ep,
    baseUrl,
    proxyInfo,
    paramValues,
    bodyText,
  });

  const methodMeta = METHOD_META[ep.method] || METHOD_META.get;
  const examples = useMemo(
    () => ep.requestBody?.content?.["application/json"]?.examples || {},
    [ep.requestBody],
  );
  const exampleKeys = useMemo(() => Object.keys(examples), [examples]);
  const simple = isSimpleEndpoint(ep);

  const initBody = useCallback(() => {
    if (exampleKeys.length > 0 && !bodyText) {
      const first = exampleKeys[0];
      setSelectedExample(first);
      setBodyText(JSON.stringify(examples[first].value, null, 2));
    }
  }, [bodyText, exampleKeys, examples]);

  const handleExampleChange = (key: string) => {
    setSelectedExample(key);
    const example = examples[key];
    if (example) setBodyText(JSON.stringify(example.value, null, 2));
  };

  const handleTryIt = () => {
    if (!expanded) {
      setExpanded(true);
      initBody();
    }
    if (simple) execute();
  };

  // A2: when the URL hash points at this endpoint card, auto-expand and scroll
  // into view. Triggers on initial mount and whenever the hash changes (back/forward,
  // index sidebar click, copy-link paste).
  useEffect(() => {
    const sync = () => {
      if (typeof window === "undefined") return;
      const hash = window.location.hash.replace(/^#/, "");
      if (hash === id) {
        setExpanded(true);
        initBody();
        // Defer scroll so the expanded body has measurable size.
        requestAnimationFrame(() => {
          rootRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
        });
      }
    };
    sync();
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, [id, initBody]);

  const handleCopyLink = useCallback(
    (event: React.MouseEvent) => {
      event.stopPropagation();
      if (typeof window === "undefined") return;
      const url = `${window.location.origin}${window.location.pathname}#${id}`;
      // history.replaceState avoids a navigation, just updates the bar so the
      // user can re-copy or share without scrolling jump.
      window.history.replaceState(null, "", `${window.location.pathname}#${id}`);
      navigator.clipboard?.writeText(url).then(
        () => {
          setCopiedAnchor(true);
          setTimeout(() => setCopiedAnchor(false), 1500);
        },
        () => {
          /* clipboard denied — URL still updated */
        },
      );
    },
    [id],
  );

  return (
    <div
      ref={rootRef}
      id={id}
      style={{
        background: "var(--bg-primary)",
        border: `1px solid var(--border-weak)`,
        borderRadius: 10,
        overflow: "hidden",
        transition: "all var(--motion-base)",
        boxShadow: expanded
          ? `0 0 0 1px ${methodMeta.glow}, var(--shadow-panel)`
          : "var(--shadow-panel)",
      }}
    >
      <div
        onClick={() => {
          setExpanded((open) => !open);
          if (!expanded) initBody();
        }}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          width: "100%",
          padding: "12px 16px",
          cursor: "pointer",
          borderLeft: `3px solid ${expanded ? methodMeta.color : "transparent"}`,
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
        {/* A2: copy a shareable deep-link to this endpoint */}
        <button
          type="button"
          onClick={handleCopyLink}
          title={copiedAnchor ? "Link copied" : "Copy link to this endpoint"}
          aria-label="Copy link to this endpoint"
          style={{
            background: "none",
            border: "none",
            padding: 4,
            cursor: "pointer",
            color: copiedAnchor ? "var(--success)" : "var(--text-faint)",
            display: "inline-flex",
            alignItems: "center",
          }}
        >
          <Link2 size={12} strokeWidth={1.5} />
        </button>

        {simple && !expanded && (
          <button
            type="button"
            className="glass-button glass-button--primary"
            onClick={(event) => {
              event.stopPropagation();
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

      {expanded && (
        <div
          style={{
            borderTop: `1px solid var(--border-weak)`,
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: 0,
          }}
        >
          <div style={{ padding: "16px 20px", borderRight: "1px solid var(--border-weak)" }}>
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

            {ep.parameters.length > 0 && (
              <div style={{ marginBottom: 16 }}>
                <SectionLabel>Parameters</SectionLabel>
                <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
                  {ep.parameters.map((param) => (
                    <div
                      key={param.name}
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
                            color: methodMeta.color,
                            fontSize: 12,
                            fontFamily: "var(--font-mono)",
                          }}
                        >
                          {param.name}
                        </code>
                        {param.required && (
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
                        {param.schema?.type || "string"}
                      </span>
                      <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
                        {param.description || ""}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

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
                      {code} <span style={{ fontWeight: 400 }}>{info.description}</span>
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>

          <div style={{ padding: "16px 20px", background: "var(--bg-secondary)", minHeight: 120 }}>
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
                type="button"
                className="glass-button glass-button--primary"
                onClick={execute}
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

            {ep.parameters.filter((param) => param.in === "path").length > 0 && (
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
                  .filter((param) => param.in === "path")
                  .map((param) => (
                    <div
                      key={param.name}
                      style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}
                    >
                      <label
                        style={{
                          fontSize: 11,
                          color: "var(--text-muted)",
                          minWidth: 80,
                          fontFamily: "var(--font-mono)",
                        }}
                      >
                        {param.name}
                      </label>
                      <input
                        type="text"
                        placeholder={
                          param.schema?.default != null ? String(param.schema.default) : param.name
                        }
                        value={paramValues[param.name] || ""}
                        onChange={(event) =>
                          setParamValues((prev) => ({ ...prev, [param.name]: event.target.value }))
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
                        onFocus={(event) => {
                          event.target.style.borderColor = "var(--border-focus)";
                        }}
                        onBlur={(event) => {
                          event.target.style.borderColor = "var(--border-weak)";
                        }}
                      />
                    </div>
                  ))}
              </div>
            )}

            {ep.requestBody && (
              <div style={{ marginBottom: 12 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
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
                      onChange={(event) => handleExampleChange(event.target.value)}
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
                      {exampleKeys.map((key) => (
                        <option key={key} value={key}>
                          {examples[key].summary || key}
                        </option>
                      ))}
                    </select>
                  )}
                </div>
                {selectedExample && examples[selectedExample]?.description && (
                  <p style={{ fontSize: 10, color: "var(--text-faint)", margin: "0 0 6px", fontStyle: "italic" }}>
                    {examples[selectedExample].description}
                  </p>
                )}
                <textarea
                  value={bodyText}
                  onChange={(event) => setBodyText(event.target.value)}
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
                  onFocus={(event) => {
                    event.target.style.borderColor = "var(--border-focus)";
                  }}
                  onBlur={(event) => {
                    event.target.style.borderColor = "var(--border-weak)";
                  }}
                />
              </div>
            )}

            {response && <ResponseViewer response={response} onCopy={copyResponse} />}

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