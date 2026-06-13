import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTransientState } from "../../hooks/useTransientState";
import { ChevronDown, Link2, Zap } from "lucide-react";

import { METHOD_META } from "@/pages/apiReference/constants";
import { MethodBadge } from "@/pages/apiReference/MethodBadge";
import { EndpointResponsesDoc } from "@/pages/apiReference/EndpointResponsesDoc";
import { EndpointTryItPanel } from "@/pages/apiReference/EndpointTryItPanel";
import { SectionLabel } from "@/pages/apiReference/SectionLabel";
import { getDefaultRequestExampleKey, isSimpleEndpoint } from "@/pages/apiReference/spec";
import type { OpenApiProxyInfo, SpecEndpoint } from "@/pages/apiReference/types";
import {
  getPathIdHint,
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
              <EndpointResponsesDoc responseEntries={responseEntries} />
            )}
          </div>

          <EndpointTryItPanel
            ep={ep}
            pathParameters={pathParameters}
            queryParameters={queryParameters}
            paramValues={paramValues}
            setParamValues={setParamValues}
            bodyText={bodyText}
            setBodyText={setBodyText}
            examples={examples}
            exampleKeys={exampleKeys}
            selectedExample={selectedExample}
            onExampleChange={handleExampleChange}
            execute={execute}
            copyCurl={copyCurl}
            loading={loading}
            response={response}
            copyResponse={copyResponse}
            downloadResponse={downloadResponse}
            simple={simple}
            proxyInfo={proxyInfo}
          />
        </div>
      )}
    </div>
  );
}
