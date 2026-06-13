import { JsonHighlight } from "@/pages/apiReference/JsonHighlight";
import { SectionLabel } from "@/pages/apiReference/SectionLabel";
import {
  responseBackground,
  responseBorder,
  responseTitle,
  responseTone,
  type ResponseEntry,
} from "@/pages/apiReference/endpointResponseHelpers";

/**
 * Read-only "Responses" documentation list for an endpoint card.
 *
 * Extracted verbatim from {@link EndpointCard}'s left column (issue #24 SRP
 * split): renders the sorted response entries with their shape name, fields,
 * id-usage hints, next-action, and example body. Pure presentation — no state,
 * no side effects — so it owns a single concern and is trivially testable.
 */
export function EndpointResponsesDoc({
  responseEntries,
}: {
  responseEntries: ResponseEntry[];
}) {
  return (
    <div>
      <SectionLabel>Responses</SectionLabel>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {responseEntries.map(([code, info]) => {
          const exampleText =
            info.example === undefined ? "" : JSON.stringify(info.example, null, 2);
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
                    <strong style={{ color: "var(--text-primary)" }}>Next:</strong>{" "}
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
  );
}
