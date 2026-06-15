/**
 * MessageFlowCard — a compact dashboard strip that visualizes the optional
 * Service Bus message flow (Actors -> Queue -> Workers -> Topic, a closed loop
 * where each completion returns to the submitting actor).
 *
 * Renders NOTHING unless the Service Bus integration is effective-enabled, so
 * the default (integration off) dashboard is unchanged. When on, it shows a
 * one-line summary of active submitters, in-flight jobs, and target clusters,
 * plus an expand control that opens the full {@link MessageFlowModal}. When the
 * integration is on but nothing is running it shows a single calm "no active
 * messages" line rather than an empty diagram.
 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, Maximize2, Radio } from "lucide-react";

import { messageFlowApi } from "@/api/messageFlow";

import { aliasTone } from "./colors";
import { MessageFlowModal } from "./MessageFlowModal";

export function MessageFlowCard() {
  const [open, setOpen] = useState(false);
  const query = useQuery({
    queryKey: ["message-flow"],
    queryFn: () => messageFlowApi.get(),
    // Poll faster while something is in flight or fading out so a status change
    // surfaces quickly; back off to a calm cadence when the queue is idle. The
    // idle cadence is kept short (10s) so a brand-new job — which appears while
    // the queue is still "idle" (visible === 0) — surfaces within one poll
    // instead of waiting a full 20s. The submit routes invalidate the backend
    // monitor/external caches, so this poll returns the fresh job immediately;
    // when nothing changed the GET is a cheap cache hit (30s server TTL).
    refetchInterval: (q) => {
      const d = q.state.data;
      const visible = (d?.active_total ?? 0) + (d?.settling_total ?? 0);
      return visible > 0 ? 8_000 : 10_000;
    },
    retry: false,
    staleTime: 5_000,
  });

  const data = query.data;
  // Hide entirely unless the integration is live (mirrors ServiceBusInboundStrip).
  if (!data || !data.enabled) return null;

  const producers = data.producers ?? [];
  const clusters = data.consumers?.clusters ?? [];
  const activeTotal = data.active_total ?? 0;
  const settlingTotal = data.settling_total ?? 0;

  return (
    <>
      <div
        className="glass-card"
        style={{
          display: "flex",
          alignItems: "center",
          gap: "var(--space-3)",
          padding: "10px 14px",
          fontSize: 12,
          color: "var(--text-muted)",
        }}
      >
        <Radio size={14} strokeWidth={1.5} style={{ color: "var(--accent)", flexShrink: 0 }} />
        <span
          style={{ color: "var(--text-primary)", fontWeight: 600 }}
          title="Active job pipeline. The Service Bus queue itself drains in under a second, so this card tracks the work in flight on AKS rather than raw queue depth."
        >
          Message Flow
        </span>
        <span style={{ fontSize: 10, color: "var(--text-faint)", textTransform: "uppercase", letterSpacing: "0.04em" }}>
          active jobs
        </span>

        {activeTotal === 0 && settlingTotal === 0 ? (
          <span style={{ color: "var(--text-faint)" }}>no active messages</span>
        ) : (
          <>
            {/* Producer color dots */}
            <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              {producers.slice(0, 6).map((p) => (
                <span
                  key={p.alias}
                  title={`${p.alias} · ${p.job_count}`}
                  style={{
                    width: 9,
                    height: 9,
                    borderRadius: "50%",
                    background: aliasTone(p.alias).accent,
                  }}
                />
              ))}
              <span style={{ marginLeft: 4 }}>
                {producers.length} submitter{producers.length === 1 ? "" : "s"}
              </span>
            </span>
            <ChevronRight size={12} strokeWidth={1.5} style={{ color: "var(--text-faint)" }} />
            <span>
              <strong style={{ color: "var(--text-primary)" }}>{activeTotal}</strong> active jobs
            </span>
            {settlingTotal > 0 ? (
              <span style={{ color: "var(--text-faint)" }}>· {settlingTotal} finishing</span>
            ) : null}
            <ChevronRight size={12} strokeWidth={1.5} style={{ color: "var(--text-faint)" }} />
            <span>
              {clusters.length} cluster{clusters.length === 1 ? "" : "s"}
            </span>
          </>
        )}

        <button
          type="button"
          className="glass-button"
          onClick={() => setOpen(true)}
          aria-label="Expand message flow"
          title="Expand"
          style={{ marginLeft: "auto", padding: 6 }}
        >
          <Maximize2 size={13} />
        </button>
      </div>

      {open ? (
        <MessageFlowModal
          snapshot={data}
          onClose={() => setOpen(false)}
          updatedAt={query.dataUpdatedAt}
        />
      ) : null}
    </>
  );
}
