import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTransientState } from "../../hooks/useTransientState";
import { ChevronDown, Copy, Link2, Loader2, Play, Zap } from "lucide-react";

import { METHOD_META } from "@/pages/apiReference/constants";
import { JsonHighlight } from "@/pages/apiReference/JsonHighlight";
import { MethodBadge } from "@/pages/apiReference/MethodBadge";
import {
  RepairPeeringButton,
  isPeerWithPlatformRecovery,
} from "@/pages/apiReference/RepairPeeringButton";
import {
  GrantLbSubnetRbacButton,
  isGrantLbSubnetRbacRecovery,
} from "@/pages/apiReference/GrantLbSubnetRbacButton";
import { ResponseViewer } from "@/pages/apiReference/ResponseViewer";
import { SectionLabel } from "@/pages/apiReference/SectionLabel";
import { getDefaultRequestExampleKey, isSimpleEndpoint } from "@/pages/apiReference/spec";
import type { OpenApiProxyInfo, SpecEndpoint } from "@/pages/apiReference/types";
import {
  getPathIdHint,
  responseBackground,
  responseBorder,
  responseTitle,
  responseTone,
  safeParseJson,
  sortResponses,
} from "@/pages/apiReference/endpointResponseHelpers";
import { useOpenApiExecutor } from "@/hooks/useOpenApiExecutor";

export function EndpointCard({
  ep,
  baseUrl,
  proxyInfo,
  dashboardApi = false,
  id,
}: {
  ep: SpecEndpoint;
  baseUrl: string;
  proxyInfo?: OpenApiProxyInfo;
  dashboardApi?: boolean;
  id: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [paramValues, setParamValues] = useState<Record<string, string>>({});
  const [bodyText, setBodyText] = useState("");
  const [selectedExample, setSelectedExample] = useState("");
  const [copiedAnchor, flashCopiedAnchor] = useTransientState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const { execute, response, loading, copyResponse, downloadResponse, copyCurl } =
    useOpenApiExecutor({
      endpoint: ep,
      baseUrl,
      proxyInfo,
      dashboardApi,
      paramValues,
      bodyText,
    });

  const methodMeta = METHOD_META[ep.method] || METHOD_META.get;
  const examples = useMemo(
    () => ep.requestBody?.content?.["application/json"]?.examples || {},
    [ep.requestBody],
  );
  const exampleKeys = useMemo(() => Object.keys(examples), [examples]);
  const pathParameters = useMemo(
    () => ep.parameters.filter((param) => param.in === "path"),
    [ep.parameters],
  );
  const queryParameters = useMemo(
    () => ep.parameters.filter((param) => param.in === "query"),
    [ep.parameters],
  );
  const simple = isSimpleEndpoint(ep);
  const responseEntries = useMemo(
    () => sortResponses(Object.entries(ep.responses || {})),
    [ep.responses],
  );
  const pathIdHint = getPathIdHint(ep.path);
  const defaultExampleKey = useMemo(
    () => getDefaultRequestExampleKey(ep, exampleKeys),
    [ep, exampleKeys],
  );

  const initBody = useCallback(() => {
    if (defaultExampleKey && !bodyText) {
      setSelectedExample(defaultExampleKey);
      setBodyText(JSON.stringify(examples[defaultExampleKey].value, null, 2));
    }
  }, [bodyText, defaultExampleKey, examples]);

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
      // The address bar now holds the shareable deep-link regardless of whether
      // the clipboard write is permitted, so flash the confirmation immediately
      // (the URL bar IS the fallback). Without this, a denied/unavailable
      // clipboard left the user with NO feedback — the button looked dead.
      flashCopiedAnchor(true, 1500);
      void navigator.clipboard?.writeText(url).catch(() => {
        /* clipboard denied — the URL bar already carries the deep-link */
      });
    },
    [flashCopiedAnchor, id],
  );

  return (
    <div
      ref={rootRef}
      id={id}
      className="endpoint-card"
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
        className="endpoint-card__header"
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
        {pathIdHint && (
          <span
            title={pathIdHint.title}
            style={{
              display: "inline-flex",
              alignItems: "center",
              padding: "2px 6px",
              borderRadius: 5,
              border: "1px solid var(--border-weak)",
              background: "var(--bg-tertiary)",
              color: "var(--text-faint)",
              fontSize: 10,
              fontFamily: "var(--font-mono)",
              fontWeight: 700,
              flexShrink: 0,
            }}
          >
            {pathIdHint.label}
          </span>
        )}
        <span
          className="endpoint-card__summary"
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
          className="endpoint-card__copylink"
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
          className="endpoint-card__body"
          style={{
            borderTop: `1px solid var(--border-weak)`,
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: 0,
          }}
        >
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

            {ep.parameters.length > 0 && (
              <div style={{ marginBottom: 16 }}>
                <SectionLabel>Parameters</SectionLabel>
                <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
                  {ep.parameters.map((param) => (
                    <div
                      key={param.name}
                      className="endpoint-card__param-row"
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
                          {param.displayName || param.name}
                        </code>
                        {param.displayName && (
                          <code
                            style={{
                              fontSize: 9,
                              color: "var(--text-faint)",
                              fontFamily: "var(--font-mono)",
                            }}
                          >
                            {param.name}
                          </code>
                        )}
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
                        {param.usageHint || param.description || ""}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {ep.responses && (
              <div>
                <SectionLabel>Responses</SectionLabel>
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {responseEntries.map(([code, info]) => {
                    const exampleText =
                      info.example === undefined
                        ? ""
                        : JSON.stringify(info.example, null, 2);
                    return (
                      <details
                        key={code}
                        open={code.startsWith("2")}
                        className="endpoint-card__response-shape"
                        style={{
                          border: `1px solid ${responseBorder(code)}`,
                          borderRadius: 8,
                          background: responseBackground(code),
                          overflow: "hidden",
                        }}
                      >
                        <summary
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: 8,
                            padding: "8px 10px",
                            cursor: "pointer",
                            listStyle: "none",
                          }}
                        >
                          <code
                            style={{
                              color: responseTone(code),
                              fontSize: 11,
                              fontFamily: "var(--font-mono)",
                              fontWeight: 800,
                            }}
                          >
                            {code}
                          </code>
                          <strong
                            style={{
                              color: "var(--text-primary)",
                              fontSize: 11,
                              fontFamily: "var(--font-mono)",
                            }}
                          >
                            {info.shapeName || responseTitle(code, info.description)}
                          </strong>
                          {info.description && (
                            <span
                              style={{
                                color: "var(--text-muted)",
                                fontSize: 11,
                                lineHeight: 1.4,
                              }}
                            >
                              {info.description}
                            </span>
                          )}
                        </summary>
                        <div
                          style={{
                            borderTop: `1px solid ${responseBorder(code)}`,
                            padding: "9px 10px 10px",
                            background: "rgba(7, 12, 22, 0.22)",
                          }}
                        >
                          {info.nextAction && (
                            <div
                              style={{
                                color: "var(--text-muted)",
                                fontSize: 11,
                                lineHeight: 1.5,
                                marginBottom: 8,
                              }}
                            >
                              <strong style={{ color: "var(--text-primary)" }}>
                                Next:
                              </strong>{" "}
                              {info.nextAction}
                            </div>
                          )}
                          {info.idUsage && info.idUsage.length > 0 && (
                            <div
                              style={{
                                display: "grid",
                                gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
                                gap: 6,
                                marginBottom: 8,
                              }}
                            >
                              {info.idUsage.map((item) => (
                                <div
                                  key={item.label}
                                  style={{
                                    padding: "7px 8px",
                                    borderRadius: 6,
                                    border: "1px solid var(--border-weak)",
                                    background: "rgba(255,255,255,0.03)",
                                  }}
                                >
                                  <div
                                    style={{
                                      color: "var(--text-primary)",
                                      fontSize: 10,
                                      fontWeight: 700,
                                      marginBottom: 3,
                                    }}
                                  >
                                    {item.label}
                                  </div>
                                  <code
                                    style={{
                                      display: "block",
                                      color: "var(--text-muted)",
                                      fontSize: 10,
                                      fontFamily: "var(--font-mono)",
                                      overflowWrap: "anywhere",
                                      marginBottom: 4,
                                    }}
                                  >
                                    {item.value}
                                  </code>
                                  <div
                                    style={{
                                      color: "var(--text-faint)",
                                      fontSize: 10,
                                      lineHeight: 1.45,
                                    }}
                                  >
                                    Use with <code>{item.useWith}</code>
                                  </div>
                                </div>
                              ))}
                            </div>
                          )}
                          {info.fields && info.fields.length > 0 && (
                            <div
                              style={{
                                display: "flex",
                                flexWrap: "wrap",
                                gap: 5,
                                marginBottom: exampleText ? 8 : 0,
                              }}
                            >
                              {info.fields.map((field) => (
                                <code
                                  key={field}
                                  style={{
                                    padding: "2px 6px",
                                    borderRadius: 4,
                                    background: "var(--bg-tertiary)",
                                    color: "var(--text-faint)",
                                    fontSize: 10,
                                    fontFamily: "var(--font-mono)",
                                  }}
                                >
                                  {field}
                                </code>
                              ))}
                            </div>
                          )}
                          {exampleText && (
                            <pre
                              style={{
                                margin: 0,
                                padding: "10px 11px",
                                maxHeight: 320,
                                overflow: "auto",
                                borderRadius: 7,
                                border: "1px solid var(--border-weak)",
                                background: "var(--bg-primary)",
                                color: "var(--text-primary)",
                                fontSize: 10,
                                lineHeight: 1.55,
                                fontFamily: "var(--font-mono)",
                                whiteSpace: "pre-wrap",
                              }}
                            >
                              <JsonHighlight text={exampleText} />
                            </pre>
                          )}
                        </div>
                      </details>
                    );
                  })}
                </div>
              </div>
            )}
          </div>

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
              <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <button
                  type="button"
                  className="glass-button"
                  onClick={copyCurl}
                  title="Copy as curl (includes a live bearer token — handle as a secret)"
                  aria-label="Copy as curl"
                  style={{ fontSize: 11, gap: 5, padding: "5px 10px" }}
                >
                  <Copy size={12} /> Copy curl
                </button>
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
            </div>

            {pathParameters.length > 0 && (
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
                {pathParameters.map((param) => (
                  <div
                    key={param.name}
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
                      {param.displayName || param.name}
                    </label>
                    <div
                      style={{
                        flex: 1,
                        display: "flex",
                        flexDirection: "column",
                        gap: 4,
                      }}
                    >
                      <input
                        type="text"
                        placeholder={
                          param.schema?.default != null
                            ? String(param.schema.default)
                            : param.name
                        }
                        value={paramValues[param.name] || ""}
                        onChange={(event) =>
                          setParamValues((prev) => ({
                            ...prev,
                            [param.name]: event.target.value,
                          }))
                        }
                        style={{
                          width: "100%",
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
                      {(param.usageHint || param.description) && (
                        <div
                          style={{
                            fontSize: 10,
                            color: "var(--text-faint)",
                            lineHeight: 1.5,
                          }}
                        >
                          {param.usageHint || param.description}
                          {param.displayName && (
                            <>
                              {" "}
                              Path variable: <code>{param.name}</code>.
                            </>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {queryParameters.length > 0 && (
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
                  Query Parameters
                </div>
                {queryParameters.map((param) => {
                  const enumValues = Array.isArray(param.schema?.enum)
                    ? (param.schema?.enum as unknown[])
                        .filter(
                          (v): v is string | number =>
                            typeof v === "string" || typeof v === "number",
                        )
                        .map(String)
                    : [];
                  const defaultValue =
                    param.schema?.default != null ? String(param.schema.default) : "";
                  const currentValue = paramValues[param.name] ?? "";
                  const sharedInputStyle: React.CSSProperties = {
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
                  };
                  return (
                    <div
                      key={param.name}
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
                        {param.name}
                      </label>
                      {enumValues.length > 0 ? (
                        <select
                          value={currentValue}
                          onChange={(event) =>
                            setParamValues((prev) => ({
                              ...prev,
                              [param.name]: event.target.value,
                            }))
                          }
                          style={{
                            ...sharedInputStyle,
                            appearance: "none",
                            WebkitAppearance: "none",
                            cursor: "pointer",
                            paddingRight: 24,
                            backgroundImage:
                              "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 24 24' fill='none' stroke='%235a6272' stroke-width='2'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E\")",
                            backgroundRepeat: "no-repeat",
                            backgroundPosition: "right 8px center",
                          }}
                          onFocus={(event) => {
                            event.target.style.borderColor = "var(--border-focus)";
                          }}
                          onBlur={(event) => {
                            event.target.style.borderColor = "var(--border-weak)";
                          }}
                        >
                          <option value="">
                            {defaultValue ? `${defaultValue} (default)` : "—"}
                          </option>
                          {enumValues.map((value) => (
                            <option key={value} value={value}>
                              {value}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <input
                          type="text"
                          placeholder={defaultValue || param.name}
                          value={currentValue}
                          onChange={(event) =>
                            setParamValues((prev) => ({
                              ...prev,
                              [param.name]: event.target.value,
                            }))
                          }
                          style={sharedInputStyle}
                          onFocus={(event) => {
                            event.target.style.borderColor = "var(--border-focus)";
                          }}
                          onBlur={(event) => {
                            event.target.style.borderColor = "var(--border-weak)";
                          }}
                        />
                      )}
                    </div>
                  );
                })}
              </div>
            )}

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
                      onChange={(event) => handleExampleChange(event.target.value)}
                      style={{
                        background: "var(--bg-primary)",
                        color: "var(--text-primary)",
                        border: "1px solid var(--border-weak)",
                        borderRadius: 5,
                        fontSize: 10,
                        padding: "2px 22px 2px 8px",
                        fontFamily: "var(--font-mono)",
                        appearance: "none",
                        WebkitAppearance: "none",
                        cursor: "pointer",
                        backgroundImage:
                          "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 24 24' fill='none' stroke='%235a6272' stroke-width='2'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E\")",
                        backgroundRepeat: "no-repeat",
                        backgroundPosition: "right 7px center",
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

            {response && (
              <ResponseViewer
                response={response}
                onCopy={copyResponse}
                onDownload={downloadResponse}
              />
            )}

            {response &&
              (response.status === 502 || response.status === 503) &&
              proxyInfo &&
              isGrantLbSubnetRbacRecovery(safeParseJson(response.body)) && (
                <GrantLbSubnetRbacButton
                  subscriptionId={proxyInfo.sub}
                  resourceGroup={proxyInfo.rg}
                  clusterName={proxyInfo.clusterName}
                  onResolved={() => execute()}
                  size="block"
                />
              )}

            {response &&
              (response.status === 502 || response.status === 503) &&
              proxyInfo &&
              !isGrantLbSubnetRbacRecovery(safeParseJson(response.body)) &&
              isPeerWithPlatformRecovery(safeParseJson(response.body)) && (
                <RepairPeeringButton
                  subscriptionId={proxyInfo.sub}
                  resourceGroup={proxyInfo.rg}
                  clusterName={proxyInfo.clusterName}
                  onResolved={() => execute()}
                  size="block"
                />
              )}

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

/** Try It response bodies are stored as strings (the executor pretty-prints
 *  JSON before display). Return parsed JSON for recovery-action detection,
 *  or null when the body is plain text / non-JSON — the caller treats null
 *  as "no recovery hint", which is the safe default. */
