/**
 * MessageFlowModal — the expanded Service Bus message-flow view.
 *
 * Renders the {@link MessageFlowConstellation} D3 force-graph as a closed loop
 * (Actors → Queue box → Workers → Topic box, with the completion looping back
 * over the top to the submitting actor) and lets the operator click or
 * keyboard-activate any job node to inspect the real JobState JSON (fetched
 * from the monitor job-detail endpoint). When there are no active messages it
 * shows a single calm notice instead of an empty graph — the integration is
 * optional and an idle queue is the normal state.
 */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import { Radio, RefreshCw, X } from "lucide-react";

import { messageFlowApi, type MessageFlowBox, type MessageFlowSnapshot, type QueueMessagePreview } from "@/api/messageFlow";
import { useRelativeTime } from "@/hooks/useRelativeTime";

import { aliasTone } from "./colors";
import { MessageFlowConstellation } from "./MessageFlowConstellation";
import { querySizeLabel } from "./layout";
import { ServiceBusTelemetryPanel } from "./ServiceBusTelemetryPanel";

interface MessageFlowModalProps {
  snapshot: MessageFlowSnapshot;
  onClose: () => void;
  /** Epoch ms of the last successful snapshot fetch (for the live "updated" badge). */
  updatedAt?: number;
  /** Force a cache-bypassing re-fetch (the manual refresh control). Omitted when
   *  the host does not support manual refresh. */
  onRefresh?: () => void | Promise<void>;
  /** True while a manual refresh is in flight (spins the icon, disables click). */
  refreshing?: boolean;
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
    // minWidth:0 lets this grid cell shrink below its content width so a long
    // value (e.g. a full UPN submitter) wraps inside the cell instead of
    // overflowing into the neighbouring column.
    <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
      <span style={{ fontSize: 10, color: "var(--text-faint)", textTransform: "uppercase", letterSpacing: "0.04em" }}>
        {label}
      </span>
      <span
        style={{
          fontSize: 12,
          color: "var(--text-primary)",
          minWidth: 0,
          overflowWrap: "anywhere",
          wordBreak: "break-word",
        }}
      >
        {value}
      </span>
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
            {box.error_code
              ? summaryItem(
                  "Error",
                  <span style={{ color: "var(--danger)" }}>{box.error_code}</span>,
                )
              : null}
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

/** Pure presentation of the peeked request-queue messages. Renders nothing when
 *  the queue is empty (the normal sub-second-drain state) so the modal stays
 *  calm; when messages linger it shows their count + sanitised content so the
 *  count/content matches the Azure portal. */
function QueueMessagesSection({ messages }: { messages: QueueMessagePreview[] }) {
  if (messages.length === 0) return null;
  return (
    <div
      style={{
        // `.glass-dialog` sets `text-align: center`, which this section would
        // otherwise inherit and centre the JSON body. Pin it back to the left so
        // the message content reads like the log/code that it is.
        textAlign: "left",
        marginTop: 18,
        paddingTop: 12,
        borderTop: "1px solid var(--glass-border)",
      }}
      data-testid="queue-messages-section"
    >
      <div
        style={{
          fontSize: 12,
          fontWeight: 600,
          color: "var(--text-primary)",
          marginBottom: 4,
        }}
      >
        Queued messages ({messages.length})
        <span
          style={{ marginLeft: 8, fontWeight: 400, fontSize: 11, color: "var(--text-faint)" }}
        >
          waiting in the request queue (peeked, not removed)
        </span>
      </div>
      <p
        style={{
          margin: "0 0 10px",
          fontSize: 11,
          lineHeight: 1.5,
          color: "var(--text-faint)",
          maxWidth: 720,
        }}
      >
        These are the raw Service Bus messages still sitting in the{" "}
        <strong style={{ color: "var(--text-muted)", fontWeight: 600 }}>request queue</strong> — each
        one is a BLAST search waiting to be picked up by a cluster worker. They are read
        non-destructively (peeked), so showing them here does not consume or remove them. The queue
        normally drains in under a second, so this list is usually empty; messages lingering here
        mean no worker has consumed them yet (cluster scaling up, paused, or at capacity). The block
        below is the message payload (query FASTA, target database, and BLAST options).
      </p>
      <div style={{ display: "grid", gap: 8 }}>
        {messages.map((m, i) => (
          <div
            key={`${m.sequence_number ?? m.message_id ?? "msg"}-${i}`}
            style={{
              display: "grid",
              gap: 4,
              padding: "8px 10px",
              borderRadius: 8,
              background: "var(--glass-surface-deep, rgba(0,0,0,0.18))",
              border: "1px solid var(--glass-border)",
            }}
          >
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", fontSize: 11 }}>
              {m.program ? (
                <span style={{ color: "var(--text-primary)", fontWeight: 600 }}>{m.program}</span>
              ) : null}
              {m.db ? <span style={{ color: "var(--text-muted)" }}>db {m.db}</span> : null}
              {m.correlation_id ? (
                <span
                  title={`correlation_id: ${m.correlation_id}`}
                  style={{ fontFamily: "var(--font-mono, monospace)", color: "var(--text-faint)" }}
                >
                  {m.correlation_id.slice(0, 12)}
                  {m.correlation_id.length > 12 ? "…" : ""}
                </span>
              ) : null}
              {m.request_id ? (
                <span
                  title={`request_id: ${m.request_id}`}
                  style={{ fontFamily: "var(--font-mono, monospace)", color: "var(--text-faint)" }}
                >
                  req {m.request_id.slice(0, 12)}
                  {m.request_id.length > 12 ? "…" : ""}
                </span>
              ) : null}
            </div>
            <pre
              style={{
                margin: 0,
                maxHeight: 220,
                overflow: "auto",
                fontSize: 11,
                lineHeight: 1.5,
                color: "var(--text-muted)",
                background: "var(--bg-code, #0d1117)",
                borderRadius: 6,
                padding: 8,
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
              }}
            >
              {m.body_preview}
            </pre>
            {m.body_truncated ? (
              <span style={{ fontSize: 10, color: "var(--text-faint)" }}>content truncated</span>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  );
}

export function MessageFlowModal({ snapshot, onClose, updatedAt, onRefresh, refreshing }: MessageFlowModalProps) {
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

  const activeTotal = snapshot.active_total ?? 0;
  const settlingTotal = snapshot.settling_total ?? 0;
  const visibleTotal = activeTotal + settlingTotal;
  const scope = snapshot.scope ?? "own";
  const scopeBadge = scope === "shared" ? "All submitters" : "Your jobs only";
  const scopeBadgeTitle =
    scope === "shared"
      ? "Showing every submitter on this deployment (BLAST_SHARED_VISIBILITY=true)."
      : "Showing only jobs your identity submitted. Other submitters' jobs are hidden.";
  const truncated = Boolean(snapshot.broker_truncated || snapshot.read_truncated);
  // Sub-second drain → operators routinely see active_message_count=0 even when
  // jobs are clearly running. Surface this honestly so the two number sets stop
  // looking contradictory.
  const activeTitle =
    activeTotal === 0
      ? "Service Bus queue depth drains in well under a second, so this counter is normally zero even while jobs are in flight. The broker nodes below are the real source of truth for active work."
      : `${activeTotal} active jobs (queued, pending, running, or reducing).`;

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
            className="glass-pill"
            style={{
              padding: "2px 8px",
              borderRadius: 999,
              fontSize: 10,
              letterSpacing: "0.04em",
              textTransform: "uppercase",
              color: scope === "shared" ? "var(--warning)" : "var(--text-muted)",
              border: "1px solid var(--glass-border)",
              flexShrink: 0,
            }}
            title={scopeBadgeTitle}
          >
            {scopeBadge}
          </span>
          {truncated ? (
            <span
              style={{
                padding: "2px 8px",
                borderRadius: 999,
                fontSize: 10,
                letterSpacing: "0.04em",
                textTransform: "uppercase",
                color: "var(--warning)",
                border: "1px solid var(--warning)",
                flexShrink: 0,
              }}
              title={
                snapshot.read_truncated
                  ? "The JobState read window was hit — counts are a floor, not the true total."
                  : `Showing the first ${snapshot.active_shown ?? 0} of ${visibleTotal} jobs to keep the graph readable.`
              }
            >
              truncated
            </span>
          ) : null}
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
              cursor: "help",
            }}
            title={activeTitle}
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
            {settlingTotal > 0 ? (
              <span style={{ color: "var(--text-faint)" }}>· {settlingTotal} finishing</span>
            ) : null}
            {updatedAgo ? (
              <span style={{ color: "var(--text-faint)" }}>· updated {updatedAgo}</span>
            ) : null}
          </span>
          {onRefresh ? (
            <button
              type="button"
              className="glass-button"
              onClick={() => {
                if (!refreshing) void onRefresh();
              }}
              disabled={refreshing}
              aria-label="Refresh message flow"
              title="Refresh now (bypass the ~30s cache)"
              style={{ padding: 6 }}
            >
              <RefreshCw
                size={14}
                className={refreshing ? "spin" : undefined}
              />
            </button>
          ) : null}
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
          {visibleTotal === 0 ? (
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
                    width: 9,
                    height: 9,
                    borderRadius: "50%",
                    background: "rgba(126, 200, 167, 0.9)",
                  }}
                />
                reducing
              </span>
              <span className="message-flow-legend__item">
                <span
                  style={{
                    position: "relative",
                    width: 10,
                    height: 10,
                    borderRadius: "50%",
                    background: "rgba(224, 123, 138, 0.4)",
                    border: "1px solid rgba(224, 123, 138, 0.92)",
                  }}
                />
                failed
              </span>
              <span className="message-flow-legend__item">
                <span
                  style={{
                    width: 9,
                    height: 9,
                    borderRadius: "50%",
                    background: "rgba(168, 173, 188, 0.5)",
                    opacity: 0.55,
                  }}
                />
                finishing (fading)
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
              <span className="message-flow-legend__item">moving dots = live energy</span>
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
                Showing the first {snapshot.active_shown ?? 0} of {visibleTotal} jobs
                to keep the graph readable.
              </div>
            ) : null}
            </>
          )}

          {/* Raw queue messages currently sitting in the request queue (peeked
              non-destructively). Distinct from the broker boxes above, which are
              in-flight BLAST JOBS — this is the actual Service Bus message
              content/count, surfaced so it matches what the Azure portal shows.
              Normally empty (sub-second drain); non-empty means messages are not
              being consumed yet. */}
          <QueueMessagesSection messages={snapshot.queue_messages ?? []} />

          {/* Service Bus telemetry footer (SRP: pure presentation panel). */}
          <ServiceBusTelemetryPanel snapshot={snapshot} />

          {/* Caption: clarify that the Queue/Topic boxes hold in-flight and
              completed JOBS, not the Service Bus queue depth above (which drains
              in well under a second so it is almost always zero), and name the
              closed-loop dual role so the two number sets do not read as
              contradictory. */}
          <div
            style={{
              marginTop: 8,
              fontSize: 10,
              lineHeight: 1.5,
              color: "var(--text-faint)",
            }}
          >
            The Queue and Topic boxes show in-flight and just-completed BLAST
            jobs; the Service Bus queue itself drains in under a second, so its
            depth above is normally zero even while jobs run. A submitter is both
            a producer and a subscriber — the dashed loop returns each completion
            to the actor that submitted it.
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
