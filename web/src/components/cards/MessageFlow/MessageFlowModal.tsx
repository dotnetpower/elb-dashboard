/**
 * MessageFlowModal — the expanded Service Bus message-flow view.
 *
 * Renders the {@link MessageFlowConstellation} D3 force-graph (Producers →
 * Broker → Consumers, with a bounded Queue/Topic broker region) and lets the
 * operator click or keyboard-activate any broker job node to inspect the real
 * JobState JSON (fetched from the monitor job-detail endpoint). When there are
 * no active messages it shows a single calm notice instead of an empty graph —
 * the integration is optional and an idle queue is the normal state.
 */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import { Radio, X } from "lucide-react";

import { messageFlowApi, type MessageFlowBox, type MessageFlowSnapshot } from "@/api/messageFlow";
import { useRelativeTime } from "@/hooks/useRelativeTime";

import { aliasTone } from "./colors";
import { MessageFlowConstellation } from "./MessageFlowConstellation";
import { querySizeLabel } from "./layout";

interface MessageFlowModalProps {
  snapshot: MessageFlowSnapshot;
  onClose: () => void;
  /** Epoch ms of the last successful snapshot fetch (for the live "updated" badge). */
  updatedAt?: number;
}

// Raw caller-identity GUIDs carry no diagnostic value and are PII the rest of
// the app deliberately redacts (see api.services.sanitise.redact_oid). Strip
// them before rendering the job JSON so the message-flow inspector never echoes
// a raw owner/tenant GUID (charter §12 — sanitise UI output). The job-detail
// endpoint returns the raw `payload` dict (which nests `metadata` and other
// sub-objects), so the redaction MUST recurse — a shallow top-level filter would
// leak a nested `payload.metadata.owner_oid`.
const REDACTED_JSON_KEYS = new Set(["owner_oid", "tenant_id"]);

function redactState(state: unknown): unknown {
  if (Array.isArray(state)) return state.map(redactState);
  if (!state || typeof state !== "object") return state;
  return Object.fromEntries(
    Object.entries(state as Record<string, unknown>)
      .filter(([key]) => !REDACTED_JSON_KEYS.has(key))
      .map(([key, value]) => [key, redactState(value)]),
  );
}

/** A small read-only key/value row used in the detail modal summary. */
function summaryItem(label: string, value: React.ReactNode) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <span style={{ fontSize: 10, color: "var(--text-faint)", textTransform: "uppercase", letterSpacing: "0.04em" }}>
        {label}
      </span>
      <span style={{ fontSize: 12, color: "var(--text-primary)" }}>{value}</span>
    </div>
  );
}

interface JobDetailModalProps {
  box: MessageFlowBox;
  onClose: () => void;
}

/** Click-through detail for a single broker box, rendered as its own modal on
 *  top of the flow modal (its own portal + higher z-index backdrop). Shows a
 *  compact summary plus the redacted JobState JSON fetched on demand. */
function JobDetailModal({ box, onClose }: JobDetailModalProps) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    // Capture phase so this handler runs before the parent flow modal's
    // Escape handler and can stop it from also closing the whole flow modal.
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  const detailQuery = useQuery({
    queryKey: ["message-flow-job", box.job_id],
    queryFn: () => messageFlowApi.getJobDetail(box.job_id),
    retry: false,
  });

  const tone = aliasTone(box.alias);

  return createPortal(
    <div
      className="glass-dialog-backdrop"
      style={{ zIndex: 1100 }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      role="dialog"
      aria-modal="true"
      aria-label={`Job detail for ${box.program ?? "blast"} ${box.job_id}`}
    >
      <div
        className="glass-card glass-card--strong glass-dialog"
        onClick={(e) => e.stopPropagation()}
        style={{
          maxWidth: 720,
          width: "calc(100vw - 48px)",
          maxHeight: "88vh",
          display: "flex",
          flexDirection: "column",
          padding: 0,
          overflow: "hidden",
          textAlign: "left",
        }}
      >
        {/* Header */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "14px 18px",
            borderBottom: "1px solid var(--glass-border)",
          }}
        >
          <span
            style={{
              width: 10,
              height: 10,
              borderRadius: 3,
              background: tone.accent,
              flexShrink: 0,
            }}
          />
          <div style={{ fontWeight: 600, color: "var(--text-primary)" }}>
            {box.program ?? "blast"}
          </div>
          <span style={{ fontSize: 11, color: "var(--text-faint)" }}>{box.job_id}</span>
          <button
            type="button"
            className="glass-button"
            onClick={onClose}
            aria-label="Close"
            style={{ marginLeft: "auto", padding: 6 }}
          >
            <X size={14} />
          </button>
        </div>

        {/* Body */}
        <div style={{ padding: 18, overflowY: "auto" }}>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))",
              gap: 12,
              marginBottom: 16,
            }}
          >
            {summaryItem("Status", `${box.status}${box.phase ? ` · ${box.phase}` : ""}`)}
            {summaryItem("Query size", querySizeLabel(box.query_size))}
            {summaryItem("Database", box.db ?? "—")}
            {summaryItem("Submitter", box.alias)}
            {summaryItem("Cluster", box.cluster_name || "unassigned")}
          </div>

          <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-primary)", marginBottom: 8 }}>
            Job JSON
          </div>
          {detailQuery.isLoading ? (
            <div style={{ color: "var(--text-muted)", fontSize: 12 }}>Loading…</div>
          ) : detailQuery.isError ? (
            <div style={{ color: "var(--warning)", fontSize: 12 }}>Could not load job detail.</div>
          ) : (
            <pre
              style={{
                margin: 0,
                maxHeight: 360,
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
      </div>
    </div>,
    document.body,
  );
}

export function MessageFlowModal({ snapshot, onClose, updatedAt }: MessageFlowModalProps) {
  const [selectedBox, setSelectedBox] = useState<MessageFlowBox | null>(null);
  const updatedAgo = useRelativeTime(updatedAt);
  // Mirrors `selectedBox` for the parent Escape handler so it can defer to the
  // detail modal (whose own capture-phase handler closes itself first).
  const detailOpenRef = useRef(false);
  useEffect(() => {
    detailOpenRef.current = selectedBox !== null;
  }, [selectedBox]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !detailOpenRef.current) onClose();
    };
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [onClose]);

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
          maxWidth: 1440,
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
          <span
            style={{
              fontSize: 11,
              color: "var(--text-faint)",
              minWidth: 0,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
            title={snapshot.namespace_fqdn}
          >
            {snapshot.namespace_fqdn}
          </span>
          <span
            style={{
              marginLeft: "auto",
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              fontSize: 11,
              color: "var(--text-muted)",
              flexShrink: 0,
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
            {updatedAgo ? (
              <span style={{ color: "var(--text-faint)" }}>· updated {updatedAgo}</span>
            ) : null}
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
            <>
            <div className="message-flow-legend">
              <span className="message-flow-legend__item">
                <span
                  style={{
                    width: 9,
                    height: 9,
                    borderRadius: "50%",
                    background: "var(--accent)",
                  }}
                />
                running
              </span>
              <span className="message-flow-legend__item">
                <span
                  style={{
                    width: 9,
                    height: 9,
                    borderRadius: "50%",
                    border: "1.5px dashed var(--text-muted)",
                  }}
                />
                queued
              </span>
              <span className="message-flow-legend__item">
                <span
                  style={{
                    width: 12,
                    height: 12,
                    borderRadius: 3,
                    background: "var(--bg-tertiary)",
                    border: "1px solid var(--border-medium)",
                  }}
                />
                api producer ·{" "}
                <span
                  style={{
                    width: 9,
                    height: 9,
                    borderRadius: "50%",
                    background: "var(--accent)",
                    display: "inline-block",
                  }}
                />{" "}
                user
              </span>
              <span className="message-flow-legend__item">node size = query length</span>
              <span className="message-flow-legend__item">color = submitter</span>
              <span className="message-flow-legend__item">link weight = message age</span>
            </div>
            <MessageFlowConstellation
              snapshot={snapshot}
              onSelectBox={setSelectedBox}
              selectedJobId={selectedBox?.job_id ?? null}
            />
            {snapshot.broker_truncated ? (
              <div
                style={{
                  marginTop: 6,
                  fontSize: 11,
                  color: "var(--text-faint)",
                  textAlign: "center",
                }}
              >
                Showing the first {snapshot.active_shown ?? 0} of {activeTotal} active
                jobs to keep the graph readable.
              </div>
            ) : null}
            </>
          )}

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

          {/* Caption: clarify that the broker nodes are in-flight JOBS, not the
              Service Bus queue depth above (which drains in well under a second
              so it is almost always zero). Without this the two number sets read
              as contradictory. */}
          <div
            style={{
              marginTop: 8,
              fontSize: 10,
              lineHeight: 1.5,
              color: "var(--text-faint)",
            }}
          >
            Broker nodes are in-flight BLAST jobs (queued/running). The Service
            Bus queue above drains in under a second, so its depth is normally
            zero even while jobs run.
          </div>
        </div>
      </div>

      {selectedBox ? (
        <JobDetailModal box={selectedBox} onClose={() => setSelectedBox(null)} />
      ) : null}
    </div>,
    document.body,
  );
}
