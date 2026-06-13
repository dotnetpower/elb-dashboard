/**
 * MessageFlowCard — a compact dashboard strip that visualizes the optional
 * Service Bus message flow (Producers -> Broker -> Consumers).
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
    refetchInterval: 20_000,
    retry: false,
    staleTime: 10_000,
  });

  const data = query.data;
  // Hide entirely unless the integration is live (mirrors ServiceBusInboundStrip).
  if (!data || !data.enabled) return null;

  const producers = data.producers ?? [];
  const clusters = data.consumers?.clusters ?? [];
  const activeTotal = data.active_total ?? 0;

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
        <span style={{ color: "var(--text-primary)", fontWeight: 600 }}>Message Flow</span>

        {activeTotal === 0 ? (
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
