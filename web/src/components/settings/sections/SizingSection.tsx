import { useEffect, useMemo, useState } from "react";

import { useSidecarMetrics, type SidecarMetric } from "@/hooks/useSidecarMetrics";
import { Group, Row, Section, StatusLine } from "@/components/settings/primitives";

/**
 * Control Plane Sizing settings section.
 *
 * Extracted verbatim from `SettingsPanel.tsx` (issue #24 SRP split). A
 * self-contained unit: it owns the sidecar resource table, the Consumption
 * pair ladder, the sustained-pressure sampling logic, and its own meter / pill
 * presentational atoms. Backed only by the `useSidecarMetrics` hook.
 */

const SIDECAR_RESOURCES: Record<string, { cpu: number; memoryGi: number }> = {
  api: { cpu: 1.0, memoryGi: 2.0 },
  frontend: { cpu: 0.25, memoryGi: 0.5 },
  worker: { cpu: 1.0, memoryGi: 2.0 },
  beat: { cpu: 0.25, memoryGi: 0.5 },
  redis: { cpu: 0.25, memoryGi: 0.5 },
  terminal: { cpu: 0.5, memoryGi: 1.0 },
};

const CONSUMPTION_PAIRS = [
  { cpu: 0.25, memoryGi: 0.5 },
  { cpu: 0.5, memoryGi: 1.0 },
  { cpu: 0.75, memoryGi: 1.5 },
  { cpu: 1.0, memoryGi: 2.0 },
  { cpu: 1.25, memoryGi: 2.5 },
  { cpu: 1.5, memoryGi: 3.0 },
  { cpu: 1.75, memoryGi: 3.5 },
  { cpu: 2.0, memoryGi: 4.0 },
  { cpu: 2.25, memoryGi: 4.5 },
  { cpu: 2.5, memoryGi: 5.0 },
  { cpu: 2.75, memoryGi: 5.5 },
  { cpu: 3.0, memoryGi: 6.0 },
  { cpu: 3.25, memoryGi: 6.5 },
  { cpu: 3.5, memoryGi: 7.0 },
  { cpu: 3.75, memoryGi: 7.5 },
  { cpu: 4.0, memoryGi: 8.0 },
];

type SizingSeverity = "ok" | "watch" | "scale";

type SidecarSizingSignal = {
  name: string;
  health: SidecarMetric["health"] | "missing";
  cpuLimit: number;
  memoryLimitGi: number;
  cpuUtilPct: number | null;
  memoryUtilPct: number | null;
  severity: SizingSeverity;
};

type SizingSample = {
  ts: number;
  signals: SidecarSizingSignal[];
};

const SIZING_HISTORY_LIMIT = 6;
const SIZING_SCALE_HIT_THRESHOLD = 3;

export function SizingSection() {
  const metrics = useSidecarMetrics();
  const rawSignals = useMemo(() => buildSizingSignals(metrics.data?.sidecars ?? {}), [metrics.data?.sidecars]);
  const [samples, setSamples] = useState<SizingSample[]>([]);
  const current = useMemo(() => currentConsumptionPair(), []);
  const next = useMemo(() => nextConsumptionPair(current.cpu, current.memoryGi), [current.cpu, current.memoryGi]);
  const snapshotTs = metrics.data?.ts ?? null;

  useEffect(() => {
    if (snapshotTs == null) return;
    setSamples((prev) => {
      if (prev.some((sample) => sample.ts === snapshotTs)) return prev;
      return [...prev, { ts: snapshotTs, signals: rawSignals }].slice(-SIZING_HISTORY_LIMIT);
    });
  }, [rawSignals, snapshotTs]);

  const signals = useMemo(
    () => applySustainedSizing(rawSignals, samples, snapshotTs),
    [rawSignals, samples, snapshotTs],
  );
  const hottest = signals.find((signal) => signal.severity === "scale") ?? signals.find((signal) => signal.severity === "watch") ?? null;
  const overall: SizingSeverity = signals.some((signal) => signal.severity === "scale")
    ? "scale"
    : signals.some((signal) => signal.severity === "watch") || metrics.isError
      ? "watch"
      : "ok";
  const statusKind = overall === "scale" ? "error" : overall === "watch" ? "info" : "success";
  const statusText = overall === "scale"
    ? "Scale up recommended"
    : overall === "watch"
      ? "Watch current load"
      : "Current size looks healthy";

  return (
    <Section heading="Control Plane Sizing">
      <Group>
        <Row
          label="Recommendation"
          hint={hottest ? `${hottest.name} is the current pressure point.` : "Based on live sidecar CPU and memory snapshots."}
          control={<SizingPill severity={overall}>{statusText}</SizingPill>}
        />
        <Row
          label="Current Consumption pair"
          hint="Azure validates the aggregate resources across all six sidecars."
          control={<code style={{ fontSize: 12 }}>{formatPair(current)}</code>}
        />
        <Row
          label="Next scale step"
          hint={next ? `Add capacity to the hottest sidecar while keeping the aggregate pair valid.` : "Already at the Consumption maximum."}
          control={<code style={{ fontSize: 12 }}>{next ? formatPair(next) : "Dedicated profile"}</code>}
        />
        <StatusLine kind={statusKind}>
          {metrics.isLoading
            ? "Waiting for the first sidecar metrics snapshot."
            : metrics.isError
              ? "Metrics are stale or unavailable; keep the current deployment but verify the sidecar reporters."
                : `${metrics.source === "live" ? "Live" : "Polling"} metrics${metrics.lastUpdated ? ` · ${metrics.lastUpdated.toLocaleTimeString()}` : ""} · ${samples.length} samples collected, ${SIZING_SCALE_HIT_THRESHOLD} needed for scale-up`}
        </StatusLine>
      </Group>

      <Group title="Sidecar pressure">
        <div style={{ display: "grid", gap: 8, padding: "12px 0" }}>
          {signals.map((signal) => (
            <div
              key={signal.name}
              style={{
                display: "grid",
                gridTemplateColumns: "96px 1fr 1fr auto",
                gap: 10,
                alignItems: "center",
                minHeight: 30,
                fontSize: 12,
              }}
            >
              <span style={{ color: "var(--text-primary)", fontWeight: 600 }}>{signal.name}</span>
              <SizingMeter label="CPU" value={signal.cpuUtilPct} limit={`${signal.cpuLimit} vCPU`} />
              <SizingMeter label="Memory" value={signal.memoryUtilPct} limit={`${signal.memoryLimitGi}Gi`} />
              <SizingPill severity={signal.severity}>{signal.severity === "scale" ? "Scale" : signal.severity === "watch" ? "Watch" : "OK"}</SizingPill>
            </div>
          ))}
        </div>
      </Group>
    </Section>
  );
}

function applySustainedSizing(
  currentSignals: SidecarSizingSignal[],
  samples: SizingSample[],
  currentTs: number | null,
): SidecarSizingSignal[] {
  const windowSamples = currentTs == null || samples.some((sample) => sample.ts === currentTs)
    ? samples
    : [...samples, { ts: currentTs, signals: currentSignals }].slice(-SIZING_HISTORY_LIMIT);

  return currentSignals.map((signal) => {
    if (signal.severity !== "scale") return signal;
    const scaleHits = windowSamples.filter((sample) => {
      const sampleSignal = sample.signals.find((candidate) => candidate.name === signal.name);
      return sampleSignal?.severity === "scale";
    }).length;
    if (scaleHits >= SIZING_SCALE_HIT_THRESHOLD) return signal;
    return { ...signal, severity: "watch" };
  });
}

function buildSizingSignals(sidecars: Record<string, SidecarMetric>): SidecarSizingSignal[] {
  return Object.entries(SIDECAR_RESOURCES).map(([name, limit]) => {
    const metric = sidecars[name];
    const cpuPct = asFiniteNumber(metric?.cpu_pct);
    const memPct = asFiniteNumber(metric?.mem_pct) ?? memoryPctFromBytes(metric, limit.memoryGi);
    const cpuUtilPct = cpuPct == null ? null : clampPct((cpuPct / (limit.cpu * 100)) * 100);
    const memoryUtilPct = memPct == null ? null : clampPct(memPct);
    const health = metric?.health ?? "missing";
    const severity = sizingSeverity(health, cpuUtilPct, memoryUtilPct);
    return {
      name,
      health,
      cpuLimit: limit.cpu,
      memoryLimitGi: limit.memoryGi,
      cpuUtilPct,
      memoryUtilPct,
      severity,
    };
  });
}

function sizingSeverity(
  health: SidecarMetric["health"] | "missing",
  cpuUtilPct: number | null,
  memoryUtilPct: number | null,
): SizingSeverity {
  if (cpuUtilPct != null && cpuUtilPct >= 85) return "scale";
  if (memoryUtilPct != null && memoryUtilPct >= 85) return "scale";
  if (health !== "ok") return "watch";
  if (cpuUtilPct != null && cpuUtilPct >= 65) return "watch";
  if (memoryUtilPct != null && memoryUtilPct >= 70) return "watch";
  return "ok";
}

function currentConsumptionPair(): { cpu: number; memoryGi: number } {
  return Object.values(SIDECAR_RESOURCES).reduce(
    (acc, value) => ({ cpu: acc.cpu + value.cpu, memoryGi: acc.memoryGi + value.memoryGi }),
    { cpu: 0, memoryGi: 0 },
  );
}

function nextConsumptionPair(cpu: number, memoryGi: number): { cpu: number; memoryGi: number } | null {
  return CONSUMPTION_PAIRS.find((pair) => pair.cpu > cpu && pair.memoryGi > memoryGi) ?? null;
}

function asFiniteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function memoryPctFromBytes(metric: SidecarMetric | undefined, memoryGi: number): number | null {
  const bytes = asFiniteNumber(metric?.mem_bytes);
  if (bytes == null || memoryGi <= 0) return null;
  return (bytes / (memoryGi * 1024 * 1024 * 1024)) * 100;
}

function clampPct(value: number): number {
  return Math.max(0, Math.min(100, Math.round(value * 10) / 10));
}

function formatPair(pair: { cpu: number; memoryGi: number }): string {
  return `${pair.cpu.toFixed(2).replace(/\.00$/, "")} CPU / ${pair.memoryGi.toFixed(1)}Gi`;
}

function SizingMeter({ label, value, limit }: { label: string; value: number | null; limit: string }) {
  const width = value == null ? 0 : value;
  const tone = value == null ? "var(--text-faint)" : value >= 85 ? "var(--danger)" : value >= 70 ? "var(--warning)" : "var(--success)";
  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, color: "var(--text-faint)", fontSize: 11, marginBottom: 4 }}>
        <span>{label}</span>
        <span>{value == null ? "No data" : `${value.toFixed(1)}%`} · {limit}</span>
      </div>
      <div style={{ height: 6, borderRadius: 999, background: "var(--bg-tertiary)", overflow: "hidden", border: "1px solid var(--border-weak)" }}>
        <div style={{ width: `${width}%`, height: "100%", background: tone }} />
      </div>
    </div>
  );
}

function SizingPill({ severity, children }: { severity: SizingSeverity; children: React.ReactNode }) {
  const color = severity === "scale" ? "var(--danger)" : severity === "watch" ? "var(--warning)" : "var(--success)";
  return (
    <span style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.04em", borderRadius: 999, padding: "2px 8px", border: "1px solid var(--border-weak)", color, background: "var(--bg-tertiary)", whiteSpace: "nowrap" }}>
      {children}
    </span>
  );
}
