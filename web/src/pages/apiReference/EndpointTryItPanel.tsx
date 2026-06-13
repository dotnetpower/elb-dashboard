import { Copy, Loader2, Play, Zap } from "lucide-react";

import {
  GrantLbSubnetRbacButton,
  isGrantLbSubnetRbacRecovery,
} from "@/pages/apiReference/GrantLbSubnetRbacButton";
import {
  RepairPeeringButton,
  isPeerWithPlatformRecovery,
} from "@/pages/apiReference/RepairPeeringButton";
import { ResponseViewer } from "@/pages/apiReference/ResponseViewer";
import { SectionLabel } from "@/pages/apiReference/SectionLabel";
import { safeParseJson } from "@/pages/apiReference/endpointResponseHelpers";
import type { OpenApiProxyInfo, SpecEndpoint, SpecParam } from "@/pages/apiReference/types";
import type { OpenApiExecutionResponse } from "@/hooks/useOpenApiExecutor";

type RequestExamples = Record<
  string,
  { summary?: string; description?: string; value: unknown }
>;

/**
 * Interactive "Try it" panel (right column) of an endpoint card.
 *
 * Extracted verbatim from {@link EndpointCard} (issue #24 SRP split): owns the
 * request-builder inputs (path/query parameters + request body editor), the
 * send/curl actions, and the response/recovery surface. All state lives in the
 * parent card; this component receives values + setters via props so the JSX
 * output is byte-identical to the pre-split render.
 */
export function EndpointTryItPanel({
  ep,
  pathParameters,
  queryParameters,
  paramValues,
  setParamValues,
  bodyText,
  setBodyText,
  examples,
  exampleKeys,
  selectedExample,
  onExampleChange,
  execute,
  copyCurl,
  loading,
  response,
  copyResponse,
  downloadResponse,
  simple,
  proxyInfo,
}: {
  ep: SpecEndpoint;
  pathParameters: SpecParam[];
  queryParameters: SpecParam[];
  paramValues: Record<string, string>;
  setParamValues: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  bodyText: string;
  setBodyText: React.Dispatch<React.SetStateAction<string>>;
  examples: RequestExamples;
  exampleKeys: string[];
  selectedExample: string;
  onExampleChange: (key: string) => void;
  execute: () => void;
  copyCurl: () => void;
  loading: boolean;
  response: OpenApiExecutionResponse | null;
  copyResponse: () => void;
  downloadResponse: () => void;
  simple: boolean;
  proxyInfo?: OpenApiProxyInfo;
}) {
  return (
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
                onChange={(event) => onExampleChange(event.target.value)}
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
  );
}
