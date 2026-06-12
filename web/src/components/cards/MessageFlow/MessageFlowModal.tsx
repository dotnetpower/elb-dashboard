/**
 * MessageFlowModal — the expanded Service Bus message-flow view.
 *
 * Lays out the three lanes (Producers -> Broker -> Consumers) and lets the
 * operator click any broker box to inspect the real JobState JSON (fetched
 * from the monitor job-detail endpoint). When there are no active messages it
 * shows a single calm notice instead of empty lanes — the integration is
 * optional and an idle queue is the normal state.
 */
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import { Radio, Server, Users, X } from "lucide-react";

import { messageFlowApi, type MessageFlowSnapshot } from "@/api/messageFlow";

import { aliasTone } from "./colors";
import { boxWidth, querySizeLabel } from "./layout";

interface MessageFlowModalProps {
  snapshot: MessageFlowSnapshot;
  onClose: () => void;
}

// Raw caller-identity GUIDs carry no diagnostic value and are PII the rest of
// the app deliberately redacts (see api.services.sanitise.redact_oid). Strip
// them before rendering the job JSON so the message-flow inspector never echoes
// a raw owner/tenant GUID (charter §12 — sanitise UI output).
const REDACTED_JSON_KEYS = new Set(["owner_oid", "tenant_id"]);

function redactState(state: unknown): unknown {
  if (!state || typeof state !== "object" || Array.isArray(state)) return state;
  return Object.fromEntries(
    Object.entries(state as Record<string, unknown>).filter(
      ([key]) => !REDACTED_JSON_KEYS.has(key),
    ),
  );
}

function laneHeader(icon: React.ReactNode, title: string, subtitle: string) {
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, color: "var(--text-primary)", fontWeight: 600 }}>
        {icon}
        {title}
      </div>
      <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 2 }}>{subtitle}</div>
    </div>
  );
}

export function MessageFlowModal({ snapshot, onClose }: MessageFlowModalProps) {
  const [selectedJob, setSelectedJob] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [onClose]);

  const detailQuery = useQuery({
    queryKey: ["message-flow-job", selectedJob],
    queryFn: () => messageFlowApi.getJobDetail(selectedJob as string),
    enabled: Boolean(selectedJob),
    retry: false,
  });

  const producers = snapshot.producers ?? [];
  const broker = snapshot.broker ?? [];
  const clusters = snapshot.consumers?.clusters ?? [];
  const counts = snapshot.sb_counts;
  const queue = counts?.queue;
  const activeTotal = snapshot.active_total ?? 0;

  return createPortal(
    <div
      className="glass-dialog-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      role="dialog"
      aria-modal="true"
      aria-label="Service Bus message flow"
    >
      <div
        className="glass-card glass-card--strong glass-dialog"
        onClick={(e) => e.stopPropagation()}
        style={{
          maxWidth: 1080,
          width: "calc(100vw - 48px)",
          maxHeight: "92vh",
          display: "flex",
          flexDirection: "column",
          padding: 0,
          overflow: "hidden",
        }}
      >
        {/* Header */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "16px 20px",
            borderBottom: "1px solid var(--glass-border)",
          }}
        >
          <Radio size={16} strokeWidth={1.5} style={{ color: "var(--accent)" }} />
          <div style={{ fontWeight: 600, color: "var(--text-primary)" }}>Message Flow</div>
          <span style={{ fontSize: 11, color: "var(--text-faint)" }}>{snapshot.namespace_fqdn}</span>
          <span
            style={{
              marginLeft: "auto",
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              fontSize: 11,
              color: "var(--text-muted)",
            }}
          >
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: "50%",
                background: activeTotal > 0 ? "var(--accent)" : "var(--text-faint)",
              }}
            />
            {activeTotal} active
          </span>
          <button
            type="button"
            className="glass-button"
            onClick={onClose}
            aria-label="Close"
            style={{ padding: 6 }}
          >
            <X size={14} />
          </button>
        </div>

        {/* Body */}
        <div style={{ padding: 20, overflowY: "auto" }}>
          {activeTotal === 0 ? (
            <div
              style={{
                padding: "40px 16px",
                textAlign: "center",
                color: "var(--text-muted)",
                fontSize: 13,
              }}
            >
              No active messages. The request queue is idle — new searches will
              appear here as they run.
            </div>
          ) : (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1fr 1.4fr 1fr",
                gap: 18,
                alignItems: "start",
              }}
            >
              {/* Producers */}
              <div>
                {laneHeader(
                  <Users size={14} strokeWidth={1.5} />,
                  "Producers",
                  snapshot.scope === "shared"
                    ? "All active submitters"
                    : "Your active submissions",
                )}
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {producers.map((p) => {
                    const tone = aliasTone(p.alias);
                    return (
                      <div
                        key={p.alias}
                        className="glass-card"
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 8,
                          padding: "8px 10px",
                          fontSize: 12,
                          borderLeft: `3px solid ${tone.accent}`,
                        }}
                      >
                        <span
                          style={{
                            width: 10,
                            height: 10,
                            borderRadius: "50%",
                            background: tone.accent,
                            flexShrink: 0,
                          }}
                        />
                        <span style={{ color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis" }}>
                          {p.alias}
                        </span>
                        <span style={{ marginLeft: "auto", color: "var(--text-muted)" }}>
                          {p.job_count}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>

              {/* Broker */}
              <div>
                {laneHeader(
                  <Radio size={14} strokeWidth={1.5} />,
                  "Broker",
                  snapshot.broker_truncated
                    ? `showing first ${snapshot.active_shown ?? broker.length} of ${activeTotal}`
                    : (snapshot.request_queue ?? "requests queue"),
                )}
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {broker.map((b) => {
                    const tone = aliasTone(b.alias);
                    return (
                      <button
                        key={b.job_id}
                        type="button"
                        onClick={() => setSelectedJob(b.job_id)}
                        title="View job JSON"
                        aria-label={`View JSON for ${b.program ?? "blast"} job (${b.status})`}
                        style={{
                          textAlign: "left",
                          cursor: "pointer",
                          width: boxWidth(b.query_size),
                          minWidth: 56,
                          maxWidth: "100%",
                          background: tone.fill,
                          border: `1px solid ${tone.border}`,
                          borderLeft: `3px solid ${tone.accent}`,
                          borderRadius: 8,
                          padding: "6px 10px",
                          color: "var(--text-primary)",
                          transition: "background 160ms ease-out",
                          outline: selectedJob === b.job_id ? `1px solid ${tone.accent}` : "none",
                        }}
                      >
                        <div style={{ fontSize: 12, fontWeight: 600 }}>
                          {b.program ?? "blast"}
                          <span style={{ fontWeight: 400, color: "var(--text-muted)" }}>
                            {" "}
                            · {querySizeLabel(b.query_size)}
                          </span>
                        </div>
                        <div style={{ fontSize: 10, color: "var(--text-faint)" }}>
                          {b.status}
                          {b.db ? ` · ${b.db}` : ""}
                        </div>
                      </button>
                    );
                  })}
                </div>
              </div>

              {/* Consumers */}
              <div>
                {laneHeader(<Server size={14} strokeWidth={1.5} />, "Consumers", "AKS clusters")}
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {clusters.map((c) => (
                    <div
                      key={`${c.subscription_id}/${c.resource_group}/${c.cluster_name}`}
                      className="glass-card"
                      style={{ padding: "10px 12px", fontSize: 12 }}
                    >
                      <div style={{ color: "var(--text-primary)", fontWeight: 600 }}>
                        {c.cluster_name || "unassigned"}
                      </div>
                      {c.resource_group ? (
                        <div style={{ fontSize: 10, color: "var(--text-faint)" }}>{c.resource_group}</div>
                      ) : null}
                      <div style={{ marginTop: 6, color: "var(--text-muted)" }}>
                        <span style={{ color: "var(--accent)" }}>● running {c.running}</span>
                        {"  "}
                        <span>○ queued {c.queued}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* JSON detail panel */}
          {selectedJob ? (
            <div className="glass-card" style={{ marginTop: 18, padding: 14 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
                <span style={{ fontWeight: 600, color: "var(--text-primary)", fontSize: 13 }}>
                  Job JSON
                </span>
                <span style={{ fontSize: 11, color: "var(--text-faint)" }}>{selectedJob}</span>
                <button
                  type="button"
                  className="glass-button"
                  onClick={() => setSelectedJob(null)}
                  style={{ marginLeft: "auto", fontSize: 11 }}
                >
                  Close
                </button>
              </div>
              {detailQuery.isLoading ? (
                <div style={{ color: "var(--text-muted)", fontSize: 12 }}>Loading…</div>
              ) : detailQuery.isError ? (
                <div style={{ color: "var(--warning)", fontSize: 12 }}>
                  Could not load job detail.
                </div>
              ) : (
                <pre
                  style={{
                    margin: 0,
                    maxHeight: 320,
                    overflow: "auto",
                    fontSize: 11,
                    lineHeight: 1.5,
                    color: "var(--text-muted)",
                    background: "var(--glass-surface-deep, rgba(0,0,0,0.18))",
                    borderRadius: 8,
                    padding: 12,
                  }}
                >
                  {JSON.stringify(redactState(detailQuery.data?.state ?? detailQuery.data), null, 2)}
                </pre>
              )}
            </div>
          ) : null}

          {/* Service Bus counts footer */}
          <div
            style={{
              marginTop: 18,
              paddingTop: 12,
              borderTop: "1px solid var(--glass-border)",
              fontSize: 11,
              color: "var(--text-muted)",
              display: "flex",
              gap: 16,
              flexWrap: "wrap",
            }}
          >
            <span>
              Service Bus:{" "}
              <strong style={{ color: "var(--text-primary)" }}>
                {snapshot.enabled ? "enabled" : "disabled"}
              </strong>
            </span>
            {counts?.available ? (
              <>
                <span>queue {queue?.active_message_count ?? "—"} active</span>
                <span>{queue?.scheduled_message_count ?? 0} scheduled</span>
                <span
                  style={{
                    color:
                      (queue?.dead_letter_message_count ?? 0) > 0
                        ? "var(--warning)"
                        : "var(--text-muted)",
                  }}
                >
                  DLQ {queue?.dead_letter_message_count ?? 0}
                </span>
              </>
            ) : (
              <span style={{ color: "var(--text-faint)" }}>
                counts unavailable{counts?.reason ? ` (${counts.reason})` : ""}
              </span>
            )}
            {snapshot.completion_topic ? (
              <span style={{ marginLeft: "auto" }}>
                completions topic: {snapshot.completion_topic}
              </span>
            ) : null}
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}
