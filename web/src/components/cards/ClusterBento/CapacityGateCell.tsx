/**
 * Capacity-gate cell for the BLAST cluster bento layout (issue #23 Stage 4).
 *
 * Reads `/api/blast/capacity` to surface the live slot count, CPU / memory
 * pressure (request percentage vs watermark), pending pods, and the
 * gate's would-be decision. Renders the disabled state when
 * `BLAST_GATE_ENABLED=false` on the api sidecar so operators can preview
 * what the gate would decide before flipping it.
 *
 * Pure helper `capacityGateBandClass` lives in `web/src/api/blast.ts` so
 * unit tests can import it without spinning up jsdom.
 */

import { useQuery } from "@tanstack/react-query";
import { Activity, ShieldAlert, ShieldCheck, ShieldOff } from "lucide-react";

import { blastApi } from "@/api/endpoints";
import { capacityGateBandClass } from "@/api/blast";
import type { CapacityGateSnapshot } from "@/api/blast";

import { BentoCell, Eyebrow } from "./atoms";

interface Props {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
  isRunning: boolean;
  program?: string;
  database?: string;
}

const CAPACITY_POLL_INTERVAL_MS = 30_000;

export function CapacityGateCell({
  subscriptionId,
  resourceGroup,
  clusterName,
  isRunning,
  program,
  database,
}: Props) {
  const query = useQuery({
    queryKey: [
      "blast-capacity",
      subscriptionId,
      resourceGroup,
      clusterName,
      program ?? "blastn",
      database ?? "nt",
    ],
    queryFn: () =>
      blastApi.getCapacityGate({
        subscriptionId,
        resourceGroup,
        clusterName,
        program,
        database,
      }),
    enabled: isRunning,
    staleTime: 15_000,
    refetchInterval: isRunning ? CAPACITY_POLL_INTERVAL_MS : false,
    retry: 0,
  });

  const snapshot = query.data?.data;
  if (!snapshot) {
    return (
      <BentoCell>
        <Eyebrow>Capacity Gate</Eyebrow>
        <div style={{ color: "var(--text-faint)", fontSize: 11 }}>
          {query.isLoading ? "Loading…" : "—"}
        </div>
      </BentoCell>
    );
  }
  return <CapacityGateBody snapshot={snapshot} />;
}

function CapacityGateBody({ snapshot }: { snapshot: CapacityGateSnapshot }) {
  const band = capacityGateBandClass(snapshot);
  const tone =
    band === "is-ok"
      ? "var(--success)"
      : band === "is-warning"
        ? "var(--warning)"
        : band === "is-danger"
          ? "var(--danger)"
          : band === "is-degraded"
            ? "var(--text-faint)"
            : "var(--text-faint)";
  const Icon = snapshot.enabled
    ? band === "is-danger" || band === "is-warning"
      ? ShieldAlert
      : ShieldCheck
    : ShieldOff;
  const stateLabel = !snapshot.enabled
    ? "Preview only"
    : snapshot.decision_preview === "admit"
      ? "Admitting"
      : "Holding";
  const slotsRatio =
    snapshot.slots.max > 0 ? snapshot.slots.in_use / snapshot.slots.max : 0;

  return (
    <BentoCell accent={tone}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <Eyebrow>Capacity Gate</Eyebrow>
        <span
          aria-label={`gate-state-${band}`}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 4,
            fontSize: 10,
            fontWeight: 600,
            color: tone,
            background: `${tone}1a`,
            border: `1px solid ${tone}55`,
            padding: "2px 8px",
            borderRadius: 999,
          }}
        >
          <Icon size={11} strokeWidth={2.2} />
          {stateLabel}
        </span>
      </div>

      <div
        style={{
          marginTop: 8,
          display: "grid",
          gridTemplateColumns: "auto 1fr",
          columnGap: 10,
          rowGap: 4,
          fontSize: 11,
          color: "var(--text-soft)",
        }}
      >
        <span>Slots</span>
        <span
          style={{
            fontVariantNumeric: "tabular-nums",
            color: "var(--text)",
          }}
        >
          {snapshot.slots.in_use} / {snapshot.slots.max}
        </span>
        <span>CPU</span>
        <RequestBar
          pct={snapshot.cpu_request_pct}
          watermark={snapshot.watermark_cpu_pct}
          tone={tone}
        />
        <span>Memory</span>
        <RequestBar
          pct={snapshot.memory_request_pct}
          watermark={snapshot.watermark_memory_pct}
          tone={tone}
        />
        <span>Pending</span>
        <span style={{ fontVariantNumeric: "tabular-nums", color: "var(--text)" }}>
          {snapshot.pending_pods}
        </span>
      </div>

      {snapshot.decision_reason && (
        <div
          style={{
            marginTop: 8,
            fontSize: 10,
            color: "var(--text-faint)",
            display: "inline-flex",
            alignItems: "center",
            gap: 4,
          }}
        >
          <Activity size={10} strokeWidth={2.2} />
          {snapshot.decision_reason}
        </div>
      )}
      {snapshot.signals_degraded && (
        <div style={{ marginTop: 6, fontSize: 10, color: "var(--warning)" }}>
          Signals degraded — gate readings stale
        </div>
      )}
      {snapshot.counters && (
        <div
          style={{
            marginTop: 8,
            display: "flex",
            gap: 10,
            fontSize: 10,
            color: "var(--text-faint)",
            fontVariantNumeric: "tabular-nums",
          }}
          title="Gate event counters since the worker started (revision-scoped)."
        >
          <span>admit {snapshot.counters.admit_total.toLocaleString()}</span>
          <span>·</span>
          <span>deny {snapshot.counters.deny_total.toLocaleString()}</span>
          {snapshot.counters.reserve_lost_total > 0 && (
            <>
              <span>·</span>
              <span>
                lost {snapshot.counters.reserve_lost_total.toLocaleString()}
              </span>
            </>
          )}
        </div>
      )}
      {snapshot.slots.max > 0 && slotsRatio > 0 && (
        <div
          aria-hidden="true"
          style={{
            marginTop: 8,
            height: 3,
            background: "var(--border-weak)",
            borderRadius: 2,
            overflow: "hidden",
          }}
        >
          <div
            style={{
              width: `${Math.min(100, Math.round(slotsRatio * 100))}%`,
              height: "100%",
              background: tone,
            }}
          />
        </div>
      )}
    </BentoCell>
  );
}

function RequestBar({
  pct,
  watermark,
  tone,
}: {
  pct: number;
  watermark: number;
  tone: string;
}) {
  const clamped = Math.max(0, Math.min(100, pct));
  const wm = Math.max(0, Math.min(100, watermark));
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
      }}
    >
      <div
        style={{
          position: "relative",
          flex: 1,
          height: 4,
          background: "var(--border-weak)",
          borderRadius: 2,
          overflow: "hidden",
        }}
      >
        <div
          style={{
            width: `${clamped}%`,
            height: "100%",
            background: tone,
          }}
        />
        <div
          aria-hidden="true"
          style={{
            position: "absolute",
            left: `${wm}%`,
            top: -2,
            height: 8,
            width: 1,
            background: "var(--text-faint)",
            opacity: 0.6,
          }}
        />
      </div>
      <span
        style={{
          fontVariantNumeric: "tabular-nums",
          color: "var(--text)",
          minWidth: 28,
          textAlign: "right",
        }}
      >
        {clamped}%
      </span>
    </div>
  );
}
