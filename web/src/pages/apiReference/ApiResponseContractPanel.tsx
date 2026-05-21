import { useState } from "react";
import type { ReactNode } from "react";
import {
  Activity,
  ChevronDown,
  Gauge,
  GitBranch,
  Hash,
  Route,
  Target,
} from "lucide-react";

import { JsonHighlight } from "@/pages/apiReference/JsonHighlight";

const RESPONSE_EXAMPLE = JSON.stringify(
  {
    job_id: "17dfd2825089",
    job_id_kind: "openapi",
    status: "dispatching",
    operation_status_url: "/api/operations/task-123",
    operation: {
      operation_id: "task-123",
      operation_type: "blast.submit.openapi",
      state: "accepted",
      poll_after_seconds: 5,
      links: {
        self: "/api/operations/task-123",
        target: "/api/blast/jobs/bb61858a-8cb6-4590-a2e3-c144662851f7",
      },
    },
    target: {
      resource_type: "blast_job",
      job_id_kind: "openapi",
      dashboard_job_id: "bb61858a-8cb6-4590-a2e3-c144662851f7",
      openapi_job_id: "17dfd2825089",
    },
    admission: {
      decision: "accepted",
      reason: "queued_for_blast_execution",
      queue: {
        state: "accepted",
        depth_bucket: "unknown",
        poll_after_seconds: 5,
      },
    },
    meta: {
      request_id: "01HX7V8W4D9Y3F9PZQ2QK4N7RA",
    },
  },
  null,
  2,
);

const CONTRACT_ITEMS = [
  {
    icon: Activity,
    title: "Operation",
    body: "Tracks the control-plane work that accepted the request. Poll links.self until the operation reaches a terminal state.",
    fields: ["operation_id", "operation_type", "state", "poll_after_seconds"],
  },
  {
    icon: Target,
    title: "Target",
    body: "Identifies the BLAST job resource and separates Dashboard UUIDs from short OpenAPI job ids.",
    fields: ["job_id_kind", "dashboard_job_id", "openapi_job_id"],
  },
  {
    icon: Gauge,
    title: "Admission",
    body: "Captures the point-in-time queue and capacity decision. It is a snapshot, not a guarantee that the BLAST run will finish.",
    fields: ["decision", "reason", "queue", "capacity"],
  },
  {
    icon: Hash,
    title: "Meta",
    body: "Carries request correlation data for logs, support, and retry analysis without changing the domain payload.",
    fields: ["request_id"],
  },
];

const STATUS_ROWS = [
  ["200", "Read, preflight, or legacy submit response completed successfully."],
  [
    "202",
    "The request was accepted. Poll the operation or job status link for progress.",
  ],
  [
    "400",
    "The request shape or identifier is invalid, including Dashboard UUIDs used as OpenAPI job ids.",
  ],
  ["409", "The request conflicts with the current resource state."],
  [
    "429",
    "The queue or runtime capacity should reject new work until the poll window passes.",
  ],
  [
    "5xx",
    "The control plane or execution runtime failed unexpectedly. Use meta.request_id for diagnosis.",
  ],
];

export function ApiResponseContractPanel({ loading = false }: { loading?: boolean }) {
  const [expanded, setExpanded] = useState(false);

  if (loading) return <ApiResponseContractSkeleton />;

  return (
    <section className="glass-card api-response-contract" style={{ padding: 18 }}>
      <div
        style={{
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "space-between",
          gap: 16,
          marginBottom: expanded ? 16 : 0,
        }}
      >
        <div style={{ minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 9, marginBottom: 5 }}>
            <Route size={17} style={{ color: "var(--accent)" }} />
            <h2 style={{ margin: 0, fontSize: 15, color: "var(--text-primary)" }}>
              API response contract
            </h2>
          </div>
          <p
            style={{
              margin: 0,
              color: "var(--text-muted)",
              fontSize: 12,
              lineHeight: 1.6,
            }}
          >
            BLAST submissions are asynchronous. A 2xx response means the control plane
            accepted or reported the request state; final BLAST success is determined by
            polling the operation and target job links.
          </p>
        </div>
        <div style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          <div
            className="api-response-contract__version"
            style={{
              flex: "0 0 auto",
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              padding: "5px 9px",
              borderRadius: 6,
              border: "1px solid var(--border-weak)",
              background: "var(--bg-secondary)",
              color: "var(--text-faint)",
              fontSize: 10,
              fontFamily: "var(--font-mono)",
            }}
          >
            <GitBranch size={11} /> additive v1
          </div>
          <button
            type="button"
            className="glass-button"
            onClick={() => setExpanded((open) => !open)}
            aria-expanded={expanded}
            aria-controls="api-response-contract-details"
            style={{ padding: "5px 9px", fontSize: 10 }}
          >
            {expanded ? "Hide" : "Show"}
            <ChevronDown
              size={12}
              style={{
                transform: expanded ? "rotate(180deg)" : "rotate(0deg)",
                transition: "transform var(--motion-fast)",
              }}
            />
          </button>
        </div>
      </div>

      {expanded && (
        <div id="api-response-contract-details">
          <div
            className="api-response-contract__grid"
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(4, minmax(0, 1fr))",
              gap: 10,
            }}
          >
            {CONTRACT_ITEMS.map((item) => {
              const Icon = item.icon;
              return (
                <ContractTile
                  key={item.title}
                  icon={<Icon size={15} />}
                  title={item.title}
                >
                  <p
                    style={{
                      margin: "6px 0 10px",
                      color: "var(--text-muted)",
                      fontSize: 11,
                      lineHeight: 1.55,
                    }}
                  >
                    {item.body}
                  </p>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
                    {item.fields.map((field) => (
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
                </ContractTile>
              );
            })}
          </div>

          <div
            className="api-response-contract__details"
            style={{
              display: "grid",
              gridTemplateColumns: "minmax(0, 0.95fr) minmax(0, 1.05fr)",
              gap: 14,
              marginTop: 14,
            }}
          >
            <div
              style={{
                border: "1px solid var(--border-weak)",
                borderRadius: 8,
                background: "var(--bg-secondary)",
                overflow: "hidden",
              }}
            >
              <div
                style={{
                  padding: "9px 12px",
                  borderBottom: "1px solid var(--border-weak)",
                  color: "var(--text-faint)",
                  fontSize: 10,
                  fontWeight: 700,
                  textTransform: "uppercase",
                }}
              >
                HTTP status policy
              </div>
              <div style={{ display: "grid" }}>
                {STATUS_ROWS.map(([code, meaning]) => (
                  <div
                    key={code}
                    style={{
                      display: "grid",
                      gridTemplateColumns: "52px 1fr",
                      gap: 8,
                      padding: "8px 12px",
                      borderTop: "1px solid var(--border-weak)",
                    }}
                  >
                    <code
                      style={{
                        color: statusTone(code),
                        fontSize: 11,
                        fontFamily: "var(--font-mono)",
                        fontWeight: 700,
                      }}
                    >
                      {code}
                    </code>
                    <span
                      style={{
                        color: "var(--text-muted)",
                        fontSize: 11,
                        lineHeight: 1.45,
                      }}
                    >
                      {meaning}
                    </span>
                  </div>
                ))}
              </div>
            </div>

            <div
              style={{
                border: "1px solid var(--border-weak)",
                borderRadius: 8,
                background: "var(--bg-secondary)",
                overflow: "hidden",
              }}
            >
              <div
                style={{
                  padding: "9px 12px",
                  borderBottom: "1px solid var(--border-weak)",
                  color: "var(--text-faint)",
                  fontSize: 10,
                  fontWeight: 700,
                  textTransform: "uppercase",
                }}
              >
                Response id contract
              </div>
              <pre
                style={{
                  margin: 0,
                  padding: "11px 12px",
                  maxHeight: 360,
                  overflow: "auto",
                  color: "var(--text-primary)",
                  fontSize: 10,
                  lineHeight: 1.55,
                  fontFamily: "var(--font-mono)",
                  whiteSpace: "pre-wrap",
                }}
              >
                <JsonHighlight text={RESPONSE_EXAMPLE} />
              </pre>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

function ApiResponseContractSkeleton() {
  return (
    <section
      className="glass-card api-response-contract"
      aria-label="Loading API response contract"
      aria-busy="true"
      style={{ padding: 18, display: "grid", gap: 14 }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "space-between",
          gap: 16,
        }}
      >
        <div style={{ flex: 1, minWidth: 0, display: "grid", gap: 8 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
            <SkeletonDot />
            <SkeletonLine width="220px" height={15} />
          </div>
          <SkeletonLine width="82%" height={12} />
          <SkeletonLine width="66%" height={12} />
        </div>
        <div style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          <SkeletonLine width="86px" height={24} radius={6} />
          <SkeletonLine width="58px" height={24} radius={6} />
        </div>
      </div>

      <div
        className="api-response-contract__grid"
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(4, minmax(0, 1fr))",
          gap: 10,
        }}
      >
        {[0, 1, 2, 3].map((index) => (
          <div
            key={index}
            style={{
              border: "1px solid var(--border-weak)",
              borderRadius: 8,
              background: "var(--bg-secondary)",
              padding: 12,
              display: "grid",
              gap: 9,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <SkeletonDot size={15} />
              <SkeletonLine width={index % 2 === 0 ? "58%" : "46%"} height={12} />
            </div>
            <SkeletonLine width="94%" height={10} />
            <SkeletonLine width="72%" height={10} />
            <div style={{ display: "flex", gap: 5 }}>
              <SkeletonLine width="52px" height={18} radius={4} />
              <SkeletonLine width="72px" height={18} radius={4} />
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function SkeletonLine({
  width,
  height,
  radius = 4,
}: {
  width: string;
  height: number;
  radius?: number;
}) {
  return (
    <span
      className="skeleton"
      style={{ display: "block", width, height, borderRadius: radius }}
    />
  );
}

function SkeletonDot({ size = 17 }: { size?: number }) {
  return <SkeletonLine width={`${size}px`} height={size} radius={999} />;
}

function ContractTile({
  icon,
  title,
  children,
}: {
  icon: ReactNode;
  title: string;
  children: ReactNode;
}) {
  return (
    <div
      style={{
        minWidth: 0,
        border: "1px solid var(--border-weak)",
        borderRadius: 8,
        background: "var(--bg-secondary)",
        padding: 12,
      }}
    >
      <div
        style={{ display: "flex", alignItems: "center", gap: 8, color: "var(--accent)" }}
      >
        {icon}
        <strong style={{ color: "var(--text-primary)", fontSize: 12 }}>{title}</strong>
      </div>
      {children}
    </div>
  );
}

function statusTone(code: string): string {
  if (code.startsWith("2")) return "var(--success)";
  if (code === "429" || code === "409") return "var(--warning)";
  if (code.startsWith("4") || code.startsWith("5")) return "var(--danger)";
  return "var(--text-muted)";
}
