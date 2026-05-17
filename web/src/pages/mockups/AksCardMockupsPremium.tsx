/**
 * AKS card redesign — premium proposals (round 3).
 *
 * Brief from user:
 *   * Most production traffic is the external POST /api/blast/submit
 *     pipeline — submit volume is the headline metric.
 *   * All of CPU%, Memory%, response time (p95), error rate, and active
 *     jobs must be visible at the same time, not behind tabs.
 *   * Premium SaaS aesthetic — generous spacing, refined typography,
 *     subtle gradients/shadows, calm but information-dense.
 *
 * Three layouts that take the same enriched fixture and arrange the
 * same metrics differently. All three are designed dark-first; the
 * project's `--bg-primary`/`--accent`/`--success`/`--warning`/`--danger`
 * tokens carry over to light mode.
 */

import { useState } from "react";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  Clock,
  Cpu,
  Database,
  Flame,
  MemoryStick,
  Send,
  Server,
  Square,
  TrendingDown,
  TrendingUp,
  XCircle,
  Zap,
} from "lucide-react";

/* -------------------------------------------------------------------- */
/* Fixture — richer than before so every layout has something to show.  */
/* -------------------------------------------------------------------- */

interface BlastJob {
  id: string;
  query: string; // file name / batch identifier
  db: string;
  splitsDone: number;
  splitsTotal: number;
  elapsedSec: number;
  etaSec?: number; // omitted when Pending or stalled
  state: "Pending" | "Running" | "Reducing" | "Completed" | "Failed";
  submitter: string; // "external-api" | "researcher@lab" | "scheduler"
  hits?: number; // present when finishing/finished
  note?: string; // "stalled · 92% cpu pressure", "Unschedulable", "merging"
}

interface ClusterTelemetry {
  name: string;
  region: string;
  k8sVersion: string;
  totalNodes: number;
  pools: { name: string; sku: string; nodes: number; role: "system" | "user" }[];

  /* DBs */
  readyDbs: { name: string; sizeGb: number }[];
  warmingDbs: string[];
  unavailableDbs: string[];

  /* Submit pipeline (external API) */
  submitsLast15m: number;
  submitsLast1h: number;
  submitsLast24h: number;
  submitErrors15m: number;
  submitRpm: number; // requests per minute, current window
  sparkSubmitsByMinute: number[]; // last 60 buckets

  /* Latency histogram (15m window) */
  p50ms: number;
  p95ms: number;
  p99ms: number;
  sparkP95: number[];

  /* Resource pressure */
  cpuPct: number;
  memPct: number;
  pendingPods: number;
  sparkCpu: number[];
  sparkMem: number[];

  /* Workload */
  activeJobs: number;
  completedToday: number;
  failedToday: number;
  jobs: BlastJob[]; // detailed roster (running + pending + a few recent)

  /* Verdict */
  health: "healthy" | "degraded" | "down";
  healthReason: string;

  /* Live events */
  events: { t: string; kind: "ok" | "warn" | "err"; msg: string }[];
}

const HEALTHY: ClusterTelemetry = {
  name: "elb-cluster-prod",
  region: "koreacentral",
  k8sVersion: "1.34.0",
  totalNodes: 4,
  pools: [
    { name: "system", sku: "Standard_D4s_v5", nodes: 1, role: "system" },
    { name: "user", sku: "Standard_E16s_v5", nodes: 3, role: "user" },
  ],
  readyDbs: [
    { name: "16S_ribosomal_RNA", sizeGb: 1.8 },
    { name: "nt_prok", sizeGb: 122 },
    { name: "ref_viruses_rep_genomes", sizeGb: 0.6 },
  ],
  warmingDbs: ["refseq_select_rna"],
  unavailableDbs: ["nr"],

  submitsLast15m: 187,
  submitsLast1h: 742,
  submitsLast24h: 14_318,
  submitErrors15m: 0,
  submitRpm: 12.5,
  sparkSubmitsByMinute: makeWave(60, 8, 18, 0),

  p50ms: 145,
  p95ms: 220,
  p99ms: 410,
  sparkP95: makeWave(60, 200, 240, 0),

  cpuPct: 0.42,
  memPct: 0.38,
  pendingPods: 0,
  sparkCpu: makeWave(60, 0.35, 0.48, 0),
  sparkMem: makeWave(60, 0.32, 0.42, 0),

  activeJobs: 2,
  completedToday: 138,
  failedToday: 1,
  jobs: [
    {
      id: "job-7f3a",
      query: "queries-2026-05-16-batch-12.fa",
      db: "nt_prok",
      splitsDone: 12,
      splitsTotal: 15,
      elapsedSec: 134,
      etaSec: 32,
      state: "Running",
      submitter: "external-api",
    },
    {
      id: "job-9c01",
      query: "16s-survey-clinical-2026-05.fa",
      db: "16S_ribosomal_RNA",
      splitsDone: 4,
      splitsTotal: 4,
      elapsedSec: 48,
      state: "Reducing",
      submitter: "external-api",
      note: "merging shard outputs",
    },
    {
      id: "job-8e22",
      query: "researcher-curated-2026-05-15.fa",
      db: "ref_viruses_rep_genomes",
      splitsDone: 3,
      splitsTotal: 3,
      elapsedSec: 192,
      state: "Completed",
      submitter: "researcher@lab",
      hits: 1248,
    },
  ],

  health: "healthy",
  healthReason: "All systems nominal · last error 4h ago",

  events: [
    { t: "12s ago", kind: "ok", msg: "blast-submit · job-7f3a · 198 ms" },
    { t: "34s ago", kind: "ok", msg: "blast-submit · job-9c01 · 212 ms" },
    { t: "1m ago", kind: "ok", msg: "job-8e22 completed (3m12s)" },
    { t: "1m ago", kind: "ok", msg: "blast-submit · job-8e22 · 167 ms" },
  ],
};

const DEGRADED: ClusterTelemetry = {
  name: "elb-cluster-lab",
  region: "koreacentral",
  k8sVersion: "1.33.4",
  totalNodes: 3,
  pools: [
    { name: "system", sku: "Standard_D4s_v5", nodes: 1, role: "system" },
    { name: "user", sku: "Standard_E16s_v5", nodes: 2, role: "user" },
  ],
  readyDbs: [
    { name: "16S_ribosomal_RNA", sizeGb: 1.8 },
    { name: "nt", sizeGb: 410 },
  ],
  warmingDbs: [],
  unavailableDbs: ["nr"],

  submitsLast15m: 312,
  submitsLast1h: 1_204,
  submitsLast24h: 22_870,
  submitErrors15m: 18,
  submitRpm: 20.8,
  sparkSubmitsByMinute: makeWave(60, 12, 26, 0.4),

  p50ms: 320,
  p95ms: 4_200,
  p99ms: 8_700,
  sparkP95: makeRamp(60, 230, 4_200),

  cpuPct: 0.92,
  memPct: 0.88,
  pendingPods: 4,
  sparkCpu: makeRamp(60, 0.5, 0.93),
  sparkMem: makeRamp(60, 0.45, 0.88),

  activeJobs: 8,
  completedToday: 92,
  failedToday: 14,
  jobs: [
    {
      id: "job-12a3",
      query: "external-api-batch-2026-05-16-002.fa",
      db: "nt",
      splitsDone: 0,
      splitsTotal: 20,
      elapsedSec: 0,
      state: "Pending",
      submitter: "external-api",
      note: "Unschedulable · no node has 16 cores free",
    },
    {
      id: "job-12a4",
      query: "external-api-batch-2026-05-16-003.fa",
      db: "nt",
      splitsDone: 0,
      splitsTotal: 20,
      elapsedSec: 0,
      state: "Pending",
      submitter: "external-api",
      note: "Unschedulable · queued behind job-12a3",
    },
    {
      id: "job-7f3a",
      query: "external-api-batch-2026-05-16-001.fa",
      db: "nt",
      splitsDone: 8,
      splitsTotal: 20,
      elapsedSec: 320,
      etaSec: undefined,
      state: "Running",
      submitter: "external-api",
      note: "stalled · splits 9-11 retry x2",
    },
    {
      id: "job-9c02",
      query: "external-api-batch-2026-05-15-093.fa",
      db: "nt",
      splitsDone: 14,
      splitsTotal: 20,
      elapsedSec: 412,
      etaSec: 124,
      state: "Running",
      submitter: "external-api",
    },
    {
      id: "job-9c01",
      query: "researcher-rerun-2026-05-15.fa",
      db: "nt",
      splitsDone: 6,
      splitsTotal: 20,
      elapsedSec: 248,
      etaSec: 320,
      state: "Running",
      submitter: "researcher@lab",
      note: "slow · cpu pressure 92%",
    },
    {
      id: "job-8e22",
      query: "16s-rapid-screen-2026-05-15.fa",
      db: "16S_ribosomal_RNA",
      splitsDone: 4,
      splitsTotal: 4,
      elapsedSec: 76,
      state: "Reducing",
      submitter: "external-api",
      note: "merging shard outputs",
    },
    {
      id: "job-9c03",
      query: "external-api-batch-2026-05-15-094.fa",
      db: "nt",
      splitsDone: 2,
      splitsTotal: 20,
      elapsedSec: 89,
      etaSec: 540,
      state: "Running",
      submitter: "external-api",
    },
    {
      id: "job-9c04",
      query: "external-api-batch-2026-05-15-095.fa",
      db: "nt",
      splitsDone: 0,
      splitsTotal: 20,
      elapsedSec: 0,
      state: "Pending",
      submitter: "external-api",
    },
    // recent (not active) — used by modal
    {
      id: "job-9b88",
      query: "external-api-batch-2026-05-15-091.fa",
      db: "nt",
      splitsDone: 5,
      splitsTotal: 20,
      elapsedSec: 412,
      state: "Failed",
      submitter: "external-api",
      note: "split-13 OOMKilled · 16Gi limit",
    },
    {
      id: "job-9b87",
      query: "external-api-batch-2026-05-15-090.fa",
      db: "nt",
      splitsDone: 20,
      splitsTotal: 20,
      elapsedSec: 318,
      state: "Completed",
      submitter: "external-api",
      hits: 4_872,
    },
  ],

  health: "degraded",
  healthReason:
    "API p95 4.2 s · 18 submit errors / 15m · CPU 92% · 4 pods pending",

  events: [
    { t: "12s ago", kind: "err", msg: "POST /api/blast/submit → 503 (4.2s)" },
    { t: "28s ago", kind: "err", msg: "POST /api/blast/submit → 503 (timeout)" },
    { t: "45s ago", kind: "warn", msg: "pod blast-job-12a3 Pending (Unschedulable)" },
    { t: "1m ago", kind: "err", msg: "POST /api/blast/submit → 503 (timeout)" },
    { t: "2m ago", kind: "warn", msg: "node aks-user-2 cpu pressure 92%" },
  ],
};

/** Smooth pseudo-random wave for sparklines. Deterministic per call so
 *  React renders are stable. */
function makeWave(n: number, min: number, max: number, seed: number): number[] {
  const out: number[] = [];
  for (let i = 0; i < n; i++) {
    const t = i / n;
    const w =
      0.5 +
      0.4 * Math.sin(t * 6 + seed) +
      0.1 * Math.sin(t * 17 + seed * 2);
    out.push(min + (max - min) * Math.max(0, Math.min(1, w)));
  }
  return out;
}
function makeRamp(n: number, from: number, to: number): number[] {
  const out: number[] = [];
  for (let i = 0; i < n; i++) {
    const t = i / (n - 1);
    const eased = Math.pow(t, 1.6);
    const noise = (Math.sin(i * 1.3) + Math.sin(i * 0.7)) * 0.04;
    out.push(from + (to - from) * Math.max(0, Math.min(1, eased + noise)));
  }
  return out;
}
function fmtNum(n: number): string {
  if (n >= 10_000) return (n / 1000).toFixed(1) + "k";
  if (n >= 1_000) return n.toLocaleString();
  return `${n}`;
}
function fmtMs(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}
function fmtDuration(sec: number): string {
  if (sec <= 0) return "—";
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m < 60) return `${m}m ${s.toString().padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${(m % 60).toString().padStart(2, "0")}m`;
}
function avg(xs: number[]): number {
  return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0;
}
function delta(xs: number[]): number {
  if (xs.length < 4) return 0;
  const head = avg(xs.slice(0, Math.floor(xs.length / 3)));
  const tail = avg(xs.slice(-Math.floor(xs.length / 3)));
  return head === 0 ? 0 : (tail - head) / head;
}

const CLUSTERS = [HEALTHY, DEGRADED];

/* -------------------------------------------------------------------- */
/* Atoms                                                                 */
/* -------------------------------------------------------------------- */

function Spark({
  data,
  color,
  width = 120,
  height = 32,
  fill = true,
  strokeWidth = 1.5,
  smooth = true,
}: {
  data: number[];
  color: string;
  width?: number;
  height?: number;
  fill?: boolean;
  strokeWidth?: number;
  smooth?: boolean;
}) {
  if (data.length === 0) return null;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const step = width / (data.length - 1 || 1);
  const pts = data.map(
    (v, i) => [i * step, height - ((v - min) / range) * (height - 2) - 1] as const,
  );

  // Catmull-Rom -> cubic Bezier for smooth curve
  let d = `M ${pts[0][0]} ${pts[0][1]}`;
  if (smooth && pts.length > 2) {
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[i - 1] || pts[i];
      const p1 = pts[i];
      const p2 = pts[i + 1];
      const p3 = pts[i + 2] || p2;
      const cp1x = p1[0] + (p2[0] - p0[0]) / 6;
      const cp1y = p1[1] + (p2[1] - p0[1]) / 6;
      const cp2x = p2[0] - (p3[0] - p1[0]) / 6;
      const cp2y = p2[1] - (p3[1] - p1[1]) / 6;
      d += ` C ${cp1x} ${cp1y}, ${cp2x} ${cp2y}, ${p2[0]} ${p2[1]}`;
    }
  } else {
    for (let i = 1; i < pts.length; i++) {
      d += ` L ${pts[i][0]} ${pts[i][1]}`;
    }
  }
  const id = `spark-grad-${Math.random().toString(36).slice(2, 8)}`;
  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity={0.35} />
          <stop offset="100%" stopColor={color} stopOpacity={0} />
        </linearGradient>
      </defs>
      {fill && (
        <path
          d={`${d} L ${width} ${height} L 0 ${height} Z`}
          fill={`url(#${id})`}
        />
      )}
      <path
        d={d}
        fill="none"
        stroke={color}
        strokeWidth={strokeWidth}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}

function TrendBadge({ d }: { d: number }) {
  if (Math.abs(d) < 0.02) {
    return (
      <span
        style={{
          fontSize: 10,
          color: "var(--text-faint)",
          fontVariantNumeric: "tabular-nums",
        }}
      >
        ±0%
      </span>
    );
  }
  const up = d > 0;
  const Icon = up ? TrendingUp : TrendingDown;
  // For latency / error / cpu sparklines, "up" is bad; the consumer
  // chooses whether up=warn or up=ok via the `goodWhen` prop on the
  // tile. Here we just render neutral colors and let the caller theme.
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 2,
        fontSize: 10,
        fontWeight: 600,
        fontVariantNumeric: "tabular-nums",
      }}
    >
      <Icon size={10} strokeWidth={2.2} />
      {up ? "+" : ""}
      {Math.round(d * 100)}%
    </span>
  );
}

function HealthPill({ c }: { c: ClusterTelemetry }) {
  const tone =
    c.health === "healthy"
      ? "var(--success)"
      : c.health === "degraded"
        ? "var(--warning)"
        : "var(--danger)";
  const Icon =
    c.health === "healthy"
      ? CheckCircle2
      : c.health === "degraded"
        ? AlertTriangle
        : XCircle;
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "4px 10px 4px 8px",
        borderRadius: 999,
        background: `${tone}1a`,
        border: `1px solid ${tone}55`,
        color: tone,
        fontSize: 11,
        fontWeight: 600,
        letterSpacing: "0.02em",
      }}
    >
      <Icon size={12} strokeWidth={2.2} />
      {c.health[0].toUpperCase() + c.health.slice(1)}
    </span>
  );
}

function ActionBtn({
  tone,
  children,
}: {
  tone: "warning" | "danger" | "neutral";
  children: React.ReactNode;
}) {
  const color =
    tone === "warning"
      ? "var(--warning)"
      : tone === "danger"
        ? "var(--danger)"
        : "var(--text-muted)";
  return (
    <button
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        padding: "5px 11px",
        fontSize: 11,
        fontWeight: 500,
        color,
        background: "transparent",
        border: "1px solid var(--border-medium)",
        borderRadius: 7,
        cursor: "pointer",
        letterSpacing: "0.01em",
      }}
    >
      {children}
    </button>
  );
}

function Eyebrow({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        fontSize: 10,
        fontWeight: 600,
        color: "var(--text-faint)",
        textTransform: "uppercase",
        letterSpacing: "0.12em",
      }}
    >
      {children}
    </div>
  );
}

function NumberDisplay({
  value,
  unit,
  size = "lg",
  tone,
}: {
  value: string;
  unit?: string;
  size?: "lg" | "xl" | "hero";
  tone?: string;
}) {
  const fontSize = size === "hero" ? 40 : size === "xl" ? 28 : 20;
  const weight = size === "hero" ? 600 : 700;
  return (
    <div
      style={{
        fontSize,
        fontWeight: weight,
        color: tone ?? "var(--text-primary)",
        letterSpacing: "-0.025em",
        fontVariantNumeric: "tabular-nums",
        lineHeight: 1.05,
        display: "inline-flex",
        alignItems: "baseline",
        gap: 4,
      }}
    >
      {value}
      {unit && (
        <span
          style={{
            fontSize: Math.round(fontSize * 0.4),
            color: "var(--text-faint)",
            fontWeight: 500,
          }}
        >
          {unit}
        </span>
      )}
    </div>
  );
}

function PressureBar({ pct, color }: { pct: number; color: string }) {
  return (
    <div
      style={{
        height: 4,
        background: "rgba(255,255,255,0.05)",
        borderRadius: 999,
        overflow: "hidden",
      }}
    >
      <div
        style={{
          width: `${pct * 100}%`,
          height: "100%",
          background: color,
          transition: "width 200ms ease-out",
        }}
      />
    </div>
  );
}

function PremiumChip({
  children,
  tone,
}: {
  children: React.ReactNode;
  tone: "success" | "warning" | "muted";
}) {
  const color =
    tone === "success"
      ? "var(--success)"
      : tone === "warning"
        ? "var(--warning)"
        : "var(--text-faint)";
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        padding: "3px 10px",
        borderRadius: 999,
        background: tone === "muted" ? "transparent" : `${color}14`,
        border:
          tone === "muted"
            ? "1px dashed var(--border-weak)"
            : `1px solid ${color}33`,
        color,
        fontSize: 11,
        fontWeight: 500,
        letterSpacing: "0.01em",
      }}
    >
      {children}
    </span>
  );
}

function EventLine({
  e,
  compact = false,
}: {
  e: { t: string; kind: "ok" | "warn" | "err"; msg: string };
  compact?: boolean;
}) {
  const color =
    e.kind === "err"
      ? "var(--danger)"
      : e.kind === "warn"
        ? "var(--warning)"
        : "var(--success)";
  const Icon =
    e.kind === "err" ? XCircle : e.kind === "warn" ? AlertTriangle : CheckCircle2;
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: compact ? "5px 10px" : "7px 12px",
        background: "transparent",
        borderRadius: 6,
        fontSize: 11.5,
        lineHeight: 1.4,
      }}
    >
      <Icon size={11} color={color} style={{ flexShrink: 0 }} />
      <span
        style={{
          flex: 1,
          color: "var(--text-primary)",
          fontFamily: e.msg.startsWith("POST") || e.msg.startsWith("blast-")
            ? "var(--font-mono)"
            : undefined,
          fontSize: 11,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {e.msg}
      </span>
      <span
        style={{
          fontSize: 10,
          color: "var(--text-faint)",
          flexShrink: 0,
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {e.t}
      </span>
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Variant P1 — "Editorial Spread"                                       */
/*                                                                       */
/* Hero number = today's submit count, sized like a magazine cover. A    */
/* second tier of metrics (15m / rpm / p95 / errors / CPU / Mem / jobs)  */
/* lives in a calm grid below. Health pill + Stop in the top-right.      */
/* Inspired by Linear / Vercel analytics.                                */
/* -------------------------------------------------------------------- */

function VariantP1({ c }: { c: ClusterTelemetry }) {
  const submitDelta = delta(c.sparkSubmitsByMinute);
  const p95Delta = delta(c.sparkP95);
  const cpuDelta = delta(c.sparkCpu);
  const memDelta = delta(c.sparkMem);
  return (
    <div
      style={{
        background:
          "linear-gradient(155deg, var(--bg-primary) 0%, var(--bg-secondary) 100%)",
        border: "1px solid var(--border-weak)",
        borderRadius: 16,
        overflow: "hidden",
        boxShadow:
          "0 1px 0 rgba(255,255,255,0.04) inset, 0 8px 32px rgba(0,0,0,0.25)",
      }}
    >
      {/* Top band */}
      <div
        style={{
          padding: "18px 24px 12px",
          display: "flex",
          alignItems: "flex-start",
          gap: 16,
          borderBottom: "1px solid var(--border-weak)",
        }}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              marginBottom: 6,
            }}
          >
            <Server size={14} color="var(--accent)" />
            <strong style={{ fontSize: 14, letterSpacing: "0.005em" }}>
              {c.name}
            </strong>
            <HealthPill c={c} />
          </div>
          <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
            ElasticBLAST execution environment · {c.region} · k8s {c.k8sVersion} ·{" "}
            {c.totalNodes} nodes
          </div>
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          <ActionBtn tone="neutral">Open</ActionBtn>
          <ActionBtn tone="warning">
            <Square size={11} /> Stop
          </ActionBtn>
        </div>
      </div>

      {/* Hero — submit volume */}
      <div
        style={{
          padding: "26px 24px 20px",
          display: "grid",
          gridTemplateColumns: "minmax(260px, 1.2fr) minmax(220px, 1fr)",
          gap: 32,
          alignItems: "center",
        }}
      >
        <div>
          <Eyebrow>Submit requests · last 24 h</Eyebrow>
          <div
            style={{
              display: "flex",
              alignItems: "baseline",
              gap: 14,
              marginTop: 8,
            }}
          >
            <NumberDisplay value={fmtNum(c.submitsLast24h)} size="hero" />
            <span
              style={{
                color: submitDelta > 0 ? "var(--success)" : "var(--text-muted)",
                fontSize: 13,
                fontWeight: 600,
              }}
            >
              <TrendBadge d={submitDelta} />
            </span>
          </div>
          <div
            style={{
              marginTop: 6,
              fontSize: 12,
              color: "var(--text-muted)",
              fontVariantNumeric: "tabular-nums",
            }}
          >
            {c.submitRpm.toFixed(1)} req/min · {c.submitsLast15m} last 15m ·{" "}
            <span
              style={{
                color:
                  c.submitErrors15m > 0 ? "var(--danger)" : "var(--text-muted)",
                fontWeight: c.submitErrors15m > 0 ? 600 : 500,
              }}
            >
              {c.submitErrors15m} errors
            </span>
          </div>
        </div>
        <div>
          <Spark
            data={c.sparkSubmitsByMinute}
            color="var(--accent)"
            width={300}
            height={68}
            strokeWidth={1.8}
          />
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              marginTop: 4,
              fontSize: 10,
              color: "var(--text-faint)",
              fontVariantNumeric: "tabular-nums",
            }}
          >
            <span>−60m</span>
            <span>−30m</span>
            <span>now</span>
          </div>
        </div>
      </div>

      {/* Tier 2 — six premium metric tiles */}
      <div
        style={{
          padding: "0 24px 20px",
          display: "grid",
          gridTemplateColumns: "repeat(3, 1fr)",
          gap: 14,
        }}
      >
        <P1Tile
          label="Response time · p95"
          value={fmtMs(c.p95ms)}
          sub={`p50 ${fmtMs(c.p50ms)} · p99 ${fmtMs(c.p99ms)}`}
          accent={
            c.p95ms > 2000
              ? "var(--danger)"
              : c.p95ms > 1000
                ? "var(--warning)"
                : "var(--accent)"
          }
          spark={c.sparkP95}
          trendDelta={p95Delta}
          trendBad={p95Delta > 0}
        />
        <P1Tile
          label="Errors · last 15 m"
          value={`${c.submitErrors15m}`}
          sub={`of ${c.submitsLast15m} requests`}
          accent={
            c.submitErrors15m > 5
              ? "var(--danger)"
              : c.submitErrors15m > 0
                ? "var(--warning)"
                : "var(--success)"
          }
        />
        <P1Tile
          label="Active jobs"
          value={`${c.activeJobs}`}
          sub={`${c.completedToday} done · ${c.failedToday} failed today`}
          accent="var(--warning)"
        />
        <P1Gauge
          icon={<Cpu size={12} />}
          label="CPU"
          pct={c.cpuPct}
          spark={c.sparkCpu}
          trendDelta={cpuDelta}
        />
        <P1Gauge
          icon={<MemoryStick size={12} />}
          label="Memory"
          pct={c.memPct}
          spark={c.sparkMem}
          trendDelta={memDelta}
        />
        <P1Tile
          label="Pending pods"
          value={`${c.pendingPods}`}
          sub={`${c.totalNodes} nodes · ${c.pools.length} pools`}
          accent={
            c.pendingPods > 2
              ? "var(--warning)"
              : c.pendingPods > 0
                ? "var(--warning)"
                : "var(--text-muted)"
          }
        />
      </div>

      {/* DBs row */}
      <div
        style={{
          padding: "14px 24px 18px",
          borderTop: "1px solid var(--border-weak)",
          display: "flex",
          alignItems: "center",
          gap: 12,
          flexWrap: "wrap",
        }}
      >
        <Eyebrow>Databases</Eyebrow>
        {c.readyDbs.map((db) => (
          <PremiumChip key={db.name} tone="success">
            <Flame size={10} /> {db.name}
          </PremiumChip>
        ))}
        {c.warmingDbs.map((n) => (
          <PremiumChip key={n} tone="warning">
            warming · {n}
          </PremiumChip>
        ))}
        {c.unavailableDbs.map((n) => (
          <PremiumChip key={n} tone="muted">
            {n}
          </PremiumChip>
        ))}
      </div>
    </div>
  );
}

function P1Tile({
  label,
  value,
  sub,
  accent,
  spark,
  trendDelta,
  trendBad,
}: {
  label: string;
  value: string;
  sub: string;
  accent: string;
  spark?: number[];
  trendDelta?: number;
  trendBad?: boolean;
}) {
  return (
    <div
      style={{
        padding: "14px 16px",
        background: "rgba(255,255,255,0.02)",
        border: "1px solid var(--border-weak)",
        borderRadius: 12,
        position: "relative",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          width: 3,
          bottom: 0,
          background: accent,
          opacity: 0.5,
        }}
      />
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 6,
        }}
      >
        <Eyebrow>{label}</Eyebrow>
        {trendDelta !== undefined && (
          <span
            style={{
              color: trendBad ? "var(--warning)" : "var(--text-faint)",
            }}
          >
            <TrendBadge d={trendDelta} />
          </span>
        )}
      </div>
      <NumberDisplay value={value} size="xl" />
      <div
        style={{
          fontSize: 11,
          color: "var(--text-muted)",
          marginTop: 4,
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {sub}
      </div>
      {spark && (
        <div style={{ marginTop: 6, marginLeft: -2 }}>
          <Spark data={spark} color={accent} width={220} height={28} />
        </div>
      )}
    </div>
  );
}

function P1Gauge({
  icon,
  label,
  pct,
  spark,
  trendDelta,
}: {
  icon: React.ReactNode;
  label: string;
  pct: number;
  spark: number[];
  trendDelta: number;
}) {
  const color =
    pct >= 0.85
      ? "var(--danger)"
      : pct >= 0.7
        ? "var(--warning)"
        : "var(--teal)";
  return (
    <div
      style={{
        padding: "14px 16px",
        background: "rgba(255,255,255,0.02)",
        border: "1px solid var(--border-weak)",
        borderRadius: 12,
        position: "relative",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          width: 3,
          bottom: 0,
          background: color,
          opacity: 0.5,
        }}
      />
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 6,
        }}
      >
        <Eyebrow>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
            {icon} {label}
          </span>
        </Eyebrow>
        <span style={{ color: pct > 0.7 ? "var(--warning)" : "var(--text-faint)" }}>
          <TrendBadge d={trendDelta} />
        </span>
      </div>
      <NumberDisplay value={`${Math.round(pct * 100)}`} unit="%" size="xl" tone={color} />
      <div style={{ marginTop: 8 }}>
        <PressureBar pct={pct} color={color} />
      </div>
      <div style={{ marginTop: 6, marginLeft: -2 }}>
        <Spark data={spark} color={color} width={220} height={22} />
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Variant P2 — "Telemetry Console"                                      */
/*                                                                       */
/* All six metrics are equal-status tiles arranged in a Datadog-style    */
/* 2-row grid. Each tile is rich (number + sparkline + sub-text + trend) */
/* but composition is symmetric, so the eye reads them in parallel.     */
/* The submit volume tile is doubled in width to honour its primacy.     */
/* -------------------------------------------------------------------- */

function VariantP2({ c }: { c: ClusterTelemetry }) {
  const submitDelta = delta(c.sparkSubmitsByMinute);
  const p95Delta = delta(c.sparkP95);
  return (
    <div
      style={{
        background: "var(--bg-primary)",
        border: "1px solid var(--border-weak)",
        borderRadius: 14,
        overflow: "hidden",
        boxShadow:
          "0 1px 0 rgba(255,255,255,0.03) inset, 0 4px 18px rgba(0,0,0,0.18)",
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: "14px 20px",
          display: "flex",
          alignItems: "center",
          gap: 10,
          borderBottom: "1px solid var(--border-weak)",
        }}
      >
        <Server size={14} color="var(--accent)" />
        <strong style={{ fontSize: 14 }}>{c.name}</strong>
        <HealthPill c={c} />
        <span style={{ fontSize: 11, color: "var(--text-faint)" }}>
          · {c.region} · k8s {c.k8sVersion} · {c.totalNodes} nodes
        </span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <ActionBtn tone="neutral">Open</ActionBtn>
          <ActionBtn tone="warning">
            <Square size={11} /> Stop
          </ActionBtn>
        </div>
      </div>

      {/* 6-tile telemetry grid (submit tile spans 2 cols) */}
      <div
        style={{
          padding: 16,
          display: "grid",
          gridTemplateColumns: "2fr 1fr 1fr",
          gridTemplateRows: "auto auto",
          gap: 12,
        }}
      >
        <P2Tile span={2}>
          <P2Header
            icon={<Send size={12} />}
            label="Submit requests · live"
            trend={submitDelta}
          />
          <div style={{ display: "flex", alignItems: "baseline", gap: 18, marginTop: 8 }}>
            <NumberDisplay value={fmtNum(c.submitsLast24h)} size="hero" />
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
                <strong style={{ color: "var(--text-primary)" }}>
                  {c.submitsLast15m}
                </strong>{" "}
                last 15 m ·{" "}
                <strong style={{ color: "var(--text-primary)" }}>
                  {c.submitRpm.toFixed(1)}
                </strong>{" "}
                req/min
              </div>
              <div style={{ fontSize: 11 }}>
                <span
                  style={{
                    color:
                      c.submitErrors15m > 0
                        ? "var(--danger)"
                        : "var(--success)",
                    fontWeight: 600,
                  }}
                >
                  {c.submitErrors15m} errors
                </span>
                <span style={{ color: "var(--text-muted)" }}>
                  {" "}
                  / {c.submitsLast15m} requests
                </span>
              </div>
            </div>
          </div>
          <div style={{ marginTop: 10 }}>
            <Spark
              data={c.sparkSubmitsByMinute}
              color="var(--accent)"
              width={460}
              height={52}
              strokeWidth={1.8}
            />
          </div>
        </P2Tile>

        <P2Tile>
          <P2Header
            icon={<Activity size={12} />}
            label="Response time · p95"
            trend={p95Delta}
            trendBad={p95Delta > 0}
          />
          <NumberDisplay
            value={fmtMs(c.p95ms)}
            size="xl"
            tone={
              c.p95ms > 2000
                ? "var(--danger)"
                : c.p95ms > 1000
                  ? "var(--warning)"
                  : "var(--text-primary)"
            }
          />
          <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2 }}>
            p50 {fmtMs(c.p50ms)} · p99 {fmtMs(c.p99ms)}
          </div>
          <div style={{ marginTop: 8 }}>
            <Spark
              data={c.sparkP95}
              color={c.p95ms > 2000 ? "var(--danger)" : "var(--accent)"}
              width={200}
              height={32}
            />
          </div>
        </P2Tile>

        <P2Tile>
          <P2Header
            icon={<Zap size={12} />}
            label="Active jobs"
          />
          <NumberDisplay value={`${c.activeJobs}`} size="xl" />
          <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2 }}>
            today: {c.completedToday} done ·{" "}
            <span
              style={{
                color: c.failedToday > 5 ? "var(--warning)" : "var(--text-muted)",
              }}
            >
              {c.failedToday} failed
            </span>
          </div>
        </P2Tile>

        <P2Tile>
          <P2GaugeRow
            icon={<Cpu size={12} />}
            label="CPU"
            pct={c.cpuPct}
            spark={c.sparkCpu}
          />
          <div style={{ marginTop: 12 }}>
            <P2GaugeRow
              icon={<MemoryStick size={12} />}
              label="Memory"
              pct={c.memPct}
              spark={c.sparkMem}
            />
          </div>
        </P2Tile>

        <P2Tile>
          <P2Header
            icon={<XCircle size={12} />}
            label="Errors / 15m"
          />
          <NumberDisplay
            value={`${c.submitErrors15m}`}
            size="xl"
            tone={
              c.submitErrors15m > 5
                ? "var(--danger)"
                : c.submitErrors15m > 0
                  ? "var(--warning)"
                  : "var(--success)"
            }
          />
          <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2 }}>
            {c.submitErrors15m > 0
              ? `${((c.submitErrors15m / c.submitsLast15m) * 100).toFixed(1)}% error rate`
              : "no errors in window"}
          </div>
        </P2Tile>

        <P2Tile>
          <P2Header
            icon={<Server size={12} />}
            label="Pending pods"
          />
          <NumberDisplay
            value={`${c.pendingPods}`}
            size="xl"
            tone={
              c.pendingPods > 0 ? "var(--warning)" : "var(--text-primary)"
            }
          />
          <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2 }}>
            {c.totalNodes} nodes · {c.pools.length} pools
          </div>
        </P2Tile>
      </div>

      {/* DBs strip */}
      <div
        style={{
          padding: "12px 20px 14px",
          borderTop: "1px solid var(--border-weak)",
          display: "flex",
          alignItems: "center",
          gap: 10,
          flexWrap: "wrap",
        }}
      >
        <Eyebrow>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
            <Database size={11} /> Databases
          </span>
        </Eyebrow>
        {c.readyDbs.map((db) => (
          <PremiumChip key={db.name} tone="success">
            <Flame size={10} /> {db.name}
          </PremiumChip>
        ))}
        {c.warmingDbs.map((n) => (
          <PremiumChip key={n} tone="warning">
            warming · {n}
          </PremiumChip>
        ))}
        {c.unavailableDbs.map((n) => (
          <PremiumChip key={n} tone="muted">
            {n}
          </PremiumChip>
        ))}
      </div>
    </div>
  );
}

function P2Tile({
  children,
  span = 1,
}: {
  children: React.ReactNode;
  span?: number;
}) {
  return (
    <div
      style={{
        gridColumn: span > 1 ? `span ${span}` : undefined,
        padding: "14px 16px",
        background: "rgba(255,255,255,0.02)",
        border: "1px solid var(--border-weak)",
        borderRadius: 10,
        display: "flex",
        flexDirection: "column",
        minWidth: 0,
      }}
    >
      {children}
    </div>
  );
}

function P2Header({
  icon,
  label,
  trend,
  trendBad,
}: {
  icon: React.ReactNode;
  label: string;
  trend?: number;
  trendBad?: boolean;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        marginBottom: 6,
      }}
    >
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 5,
          fontSize: 10,
          fontWeight: 600,
          color: "var(--text-muted)",
          textTransform: "uppercase",
          letterSpacing: "0.1em",
        }}
      >
        {icon}
        {label}
      </span>
      {trend !== undefined && (
        <span
          style={{
            color: trendBad ? "var(--warning)" : "var(--text-faint)",
          }}
        >
          <TrendBadge d={trend} />
        </span>
      )}
    </div>
  );
}

function P2GaugeRow({
  icon,
  label,
  pct,
  spark,
}: {
  icon: React.ReactNode;
  label: string;
  pct: number;
  spark: number[];
}) {
  const color =
    pct >= 0.85
      ? "var(--danger)"
      : pct >= 0.7
        ? "var(--warning)"
        : "var(--teal)";
  return (
    <div>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          marginBottom: 4,
        }}
      >
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 4,
            fontSize: 10,
            color: "var(--text-muted)",
            textTransform: "uppercase",
            letterSpacing: "0.08em",
            fontWeight: 600,
          }}
        >
          {icon} {label}
        </span>
        <span
          style={{
            marginLeft: "auto",
            color,
            fontSize: 13,
            fontWeight: 700,
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {Math.round(pct * 100)}%
        </span>
      </div>
      <PressureBar pct={pct} color={color} />
      <div style={{ marginTop: 4, marginLeft: -2 }}>
        <Spark data={spark} color={color} width={200} height={18} />
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Atoms shared by the redesigned P3 layout                              */
/* -------------------------------------------------------------------- */

function JobStateBadge({ s }: { s: BlastJob["state"] }) {
  const map: Record<BlastJob["state"], { color: string; bg: string }> = {
    Pending: { color: "var(--text-faint)", bg: "rgba(255,255,255,0.04)" },
    Running: { color: "var(--accent)", bg: "rgba(110,159,255,0.10)" },
    Reducing: { color: "var(--purple)", bg: "rgba(180,130,255,0.10)" },
    Completed: { color: "var(--success)", bg: "rgba(115,191,105,0.10)" },
    Failed: { color: "var(--danger)", bg: "rgba(242,114,111,0.10)" },
  };
  const m = map[s];
  return (
    <span
      style={{
        fontSize: 10,
        fontWeight: 600,
        letterSpacing: "0.04em",
        padding: "2px 7px",
        borderRadius: 4,
        background: m.bg,
        color: m.color,
        border: `1px solid ${m.color}33`,
      }}
    >
      {s.toUpperCase()}
    </span>
  );
}

/** Tiny segmented progress (10 segments). Reads at a glance, no SVG. */
function SplitProgress({
  done,
  total,
  color = "var(--accent)",
}: {
  done: number;
  total: number;
  color?: string;
}) {
  const pct = total === 0 ? 0 : done / total;
  return (
    <div
      style={{
        display: "flex",
        gap: 2,
        width: 76,
        height: 4,
        alignItems: "center",
      }}
      title={`${done} / ${total} splits`}
    >
      {Array.from({ length: 10 }).map((_, i) => {
        const filled = i < Math.round(pct * 10);
        return (
          <div
            key={i}
            style={{
              flex: 1,
              height: "100%",
              borderRadius: 1,
              background: filled ? color : "rgba(255,255,255,0.07)",
            }}
          />
        );
      })}
    </div>
  );
}

/** Compact one-line job entry for the bento "Active jobs" cell. */
function JobRow({ j, dense = false }: { j: BlastJob; dense?: boolean }) {
  const tone =
    j.state === "Failed"
      ? "var(--danger)"
      : j.state === "Pending"
        ? "var(--text-faint)"
        : j.state === "Reducing"
          ? "var(--purple)"
          : j.state === "Completed"
            ? "var(--success)"
            : "var(--accent)";
  // Subtle state-tinted background — unified palette, no left bar.
  const rowBg =
    j.state === "Failed"
      ? "rgba(242,114,111,0.07)"
      : j.state === "Reducing"
        ? "rgba(184,119,217,0.08)"
        : j.state === "Running"
          ? "rgba(110,159,255,0.07)"
          : j.state === "Completed"
            ? "rgba(115,191,105,0.06)"
            : "rgba(255,255,255,0.025)"; // Pending
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "82px 1fr 90px 80px auto",
        alignItems: "center",
        gap: 12,
        padding: dense ? "6px 10px" : "8px 12px",
        borderRadius: 7,
        background: rowBg,
        border: "1px solid rgba(255,255,255,0.04)",
        fontSize: 11.5,
      }}
    >
      <span
        style={{
          fontFamily: "var(--font-mono)",
          fontSize: 11,
          color: "var(--text-primary)",
          fontWeight: 500,
        }}
      >
        {j.id}
      </span>
      <span
        style={{
          color: "var(--text-muted)",
          fontSize: 11,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        <span style={{ color: "var(--text-primary)", fontWeight: 500 }}>
          {j.db}
        </span>
        {j.note ? (
          <span style={{ color: tone, marginLeft: 6 }}>· {j.note}</span>
        ) : (
          <span style={{ marginLeft: 6 }}>· {j.query}</span>
        )}
      </span>
      <SplitProgress done={j.splitsDone} total={j.splitsTotal} color={tone} />
      <span
        style={{
          fontVariantNumeric: "tabular-nums",
          fontSize: 11,
          color: "var(--text-muted)",
          textAlign: "right",
        }}
      >
        {j.state === "Pending"
          ? "queued"
          : j.etaSec
            ? `${fmtDuration(j.elapsedSec)} · ETA ${fmtDuration(j.etaSec)}`
            : fmtDuration(j.elapsedSec)}
      </span>
      <JobStateBadge s={j.state} />
    </div>
  );
}

/**
 * One inline KPI used inside the compact "Pulse" strip. Replaces the
 * previously chunky CPU/Memory cells with a clean horizontal layout
 * where the bar IS the visual — no separate sparkline below.
 */
function KpiInline({
  icon,
  label,
  value,
  tone,
  bar,
  hint,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  tone: string;
  /** When provided renders a thin pressure bar on the right instead of free space. */
  bar?: number;
  hint?: string;
}) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 6,
        flex: 1,
        minWidth: 0,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          fontSize: 10,
          color: "var(--text-muted)",
          textTransform: "uppercase",
          letterSpacing: "0.08em",
          fontWeight: 600,
        }}
      >
        {icon}
        <span>{label}</span>
      </div>
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          gap: 8,
        }}
      >
        <span
          style={{
            fontSize: 18,
            fontWeight: 700,
            color: tone,
            fontVariantNumeric: "tabular-nums",
            letterSpacing: "-0.01em",
            lineHeight: 1,
          }}
        >
          {value}
        </span>
        {hint && (
          <span
            style={{
              fontSize: 10,
              color: "var(--text-faint)",
              fontVariantNumeric: "tabular-nums",
            }}
          >
            {hint}
          </span>
        )}
      </div>
      {bar !== undefined && (
        <PressureBar pct={Math.min(1, bar)} color={tone} />
      )}
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Variant P3 — "Mission Control Bento"                                  */
/*                                                                       */
/* Bento-grid: large hero submit panel + smaller specialist cells +      */
/* always-on live activity feed. Designed to feel like a NOC / mission   */
/* control screen but with a calm palette. The activity feed is the     */
/* key for catching external API failures the moment they happen.       */
/* -------------------------------------------------------------------- */

function VariantP3({ c }: { c: ClusterTelemetry }) {
  const [modalOpen, setModalOpen] = useState(false);
  const submitDelta = delta(c.sparkSubmitsByMinute);
  const cpuTone =
    c.cpuPct >= 0.85
      ? "var(--danger)"
      : c.cpuPct >= 0.7
        ? "var(--warning)"
        : "var(--teal)";
  const memTone =
    c.memPct >= 0.85
      ? "var(--danger)"
      : c.memPct >= 0.7
        ? "var(--warning)"
        : "var(--teal)";
  const p95Tone =
    c.p95ms > 2000
      ? "var(--danger)"
      : c.p95ms > 1000
        ? "var(--warning)"
        : "var(--text-primary)";
  const errTone =
    c.submitErrors15m > 5
      ? "var(--danger)"
      : c.submitErrors15m > 0
        ? "var(--warning)"
        : "var(--success)";
  const errRatePct =
    c.submitsLast15m === 0
      ? 0
      : (c.submitErrors15m / c.submitsLast15m) * 100;

  const activeRoster = c.jobs.filter(
    (j) => j.state === "Pending" || j.state === "Running" || j.state === "Reducing",
  );
  const previewRoster = activeRoster.slice(0, 4);
  const overflowJobs = Math.max(0, activeRoster.length - previewRoster.length);

  return (
    <div
      style={{
        background:
          "linear-gradient(180deg, var(--bg-primary) 0%, rgba(20,22,28,0.95) 100%)",
        border: "1px solid var(--border-weak)",
        borderRadius: 16,
        overflow: "hidden",
        boxShadow:
          "0 1px 0 rgba(255,255,255,0.04) inset, 0 8px 32px rgba(0,0,0,0.28)",
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: "14px 20px",
          display: "flex",
          alignItems: "center",
          gap: 12,
          borderBottom: "1px solid var(--border-weak)",
        }}
      >
        <Server size={14} color="var(--accent)" />
        <strong style={{ fontSize: 14 }}>{c.name}</strong>
        <HealthPill c={c} />
        {c.health !== "healthy" && (
          <span
            style={{
              fontSize: 11,
              color: "var(--warning)",
              fontWeight: 500,
            }}
          >
            · {c.healthReason}
          </span>
        )}
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <button
            onClick={() => setModalOpen(true)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 5,
              padding: "5px 11px",
              fontSize: 11,
              fontWeight: 500,
              color: "var(--text-primary)",
              background: "var(--accent)",
              border: "1px solid var(--accent)",
              borderRadius: 7,
              cursor: "pointer",
              letterSpacing: "0.01em",
            }}
          >
            Open <ChevronRight size={11} />
          </button>
          <ActionBtn tone="warning">
            <Square size={11} /> Stop
          </ActionBtn>
        </div>
      </div>

      {/* Bento grid:
            r1-r2 c1-c2 = Hero submit (compact sparkline)
            r1-r3 c3    = Live activity (always on)
            r3    c1-c2 = Pulse strip (p95 / errors / CPU / Memory)
            r4    c1-c3 = Active jobs (full width)
      */}
      <div
        style={{
          padding: 14,
          display: "grid",
          gridTemplateColumns: "2fr 1fr 1fr",
          gridTemplateRows: "auto auto auto auto",
          gap: 12,
        }}
      >
        {/* Hero submit */}
        <BentoCell span={[2, 2]} accent="var(--accent)">
          <Eyebrow>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
              <Send size={11} /> Submit pipeline · 24 h
            </span>
          </Eyebrow>
          <div
            style={{
              display: "flex",
              alignItems: "baseline",
              gap: 16,
              marginTop: 10,
            }}
          >
            <NumberDisplay value={fmtNum(c.submitsLast24h)} size="hero" />
            <span
              style={{
                color:
                  submitDelta >= 0 ? "var(--success)" : "var(--text-muted)",
              }}
            >
              <TrendBadge d={submitDelta} />
            </span>
          </div>
          <div
            style={{
              display: "flex",
              gap: 24,
              marginTop: 8,
              fontSize: 12,
              color: "var(--text-muted)",
              fontVariantNumeric: "tabular-nums",
            }}
          >
            <span>
              <strong style={{ color: "var(--text-primary)" }}>
                {c.submitsLast15m}
              </strong>{" "}
              · 15 m
            </span>
            <span>
              <strong style={{ color: "var(--text-primary)" }}>
                {c.submitsLast1h}
              </strong>{" "}
              · 1 h
            </span>
            <span>
              <strong style={{ color: "var(--text-primary)" }}>
                {c.submitRpm.toFixed(1)}
              </strong>{" "}
              req/min
            </span>
            <span
              style={{
                color:
                  c.submitErrors15m > 0 ? "var(--danger)" : "var(--success)",
                fontWeight: c.submitErrors15m > 0 ? 600 : 500,
              }}
            >
              <strong>{c.submitErrors15m}</strong> errors
            </span>
          </div>
          <div
            style={{
              marginTop: 12,
              display: "flex",
              flexDirection: "column",
              gap: 4,
            }}
          >
            <Spark
              data={c.sparkSubmitsByMinute}
              color="var(--accent)"
              width={520}
              height={52}
              strokeWidth={1.5}
            />
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                fontSize: 10,
                color: "var(--text-faint)",
                fontVariantNumeric: "tabular-nums",
              }}
            >
              <span>−60m</span>
              <span>−45m</span>
              <span>−30m</span>
              <span>−15m</span>
              <span>now</span>
            </div>
          </div>
        </BentoCell>

        {/* Live activity */}
        <BentoCell span={[1, 4]} accent="var(--text-faint)">
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              marginBottom: 10,
            }}
          >
            <Eyebrow>
              <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
                <Activity size={11} /> Live activity
              </span>
            </Eyebrow>
            <span
              style={{
                width: 6,
                height: 6,
                borderRadius: "50%",
                background: "var(--success)",
                boxShadow: "0 0 0 3px rgba(115,191,105,0.2)",
                animation: "elbPulse 1.6s ease-in-out infinite",
              }}
            />
          </div>
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 5,
              flex: 1,
            }}
          >
            {c.events.map((e, i) => (
              <EventLine key={i} e={e} compact />
            ))}
          </div>
          <div
            style={{
              marginTop: 10,
              fontSize: 10,
              color: "var(--text-faint)",
              display: "flex",
              alignItems: "center",
              gap: 4,
            }}
          >
            <Clock size={10} /> last 5 minutes
          </div>
        </BentoCell>

        {/* Pulse strip — the cleaned-up CPU/Mem/p95/errors row */}
        <BentoCell span={[2, 1]} accent="var(--teal)">
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "1fr 1fr 1fr 1fr",
              gap: 18,
              alignItems: "stretch",
            }}
          >
            <KpiInline
              icon={<Activity size={11} />}
              label="API p95"
              value={fmtMs(c.p95ms)}
              tone={p95Tone}
              hint={`p50 ${fmtMs(c.p50ms)}`}
            />
            <KpiInline
              icon={<XCircle size={11} />}
              label="Errors · 15 m"
              value={`${c.submitErrors15m}`}
              tone={errTone}
              hint={
                c.submitErrors15m > 0
                  ? `${errRatePct.toFixed(1)}% rate`
                  : "clean"
              }
            />
            <KpiInline
              icon={<Cpu size={11} />}
              label="CPU"
              value={`${Math.round(c.cpuPct * 100)}%`}
              tone={cpuTone}
              bar={c.cpuPct}
              hint={c.pendingPods > 0 ? `${c.pendingPods} pending` : undefined}
            />
            <KpiInline
              icon={<MemoryStick size={11} />}
              label="Memory"
              value={`${Math.round(c.memPct * 100)}%`}
              tone={memTone}
              bar={c.memPct}
            />
          </div>
        </BentoCell>

        {/* Active jobs — replaces the old separate CPU/Mem cells */}
        <BentoCell span={[2, 1]} accent="var(--accent)">
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              marginBottom: 10,
            }}
          >
            <Eyebrow>
              <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
                <Zap size={11} /> Active jobs · {activeRoster.length}
              </span>
            </Eyebrow>
            <button
              onClick={() => setModalOpen(true)}
              style={{
                fontSize: 10,
                color: "var(--text-muted)",
                background: "transparent",
                border: "none",
                cursor: "pointer",
                fontWeight: 500,
              }}
            >
              View all →
            </button>
          </div>
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 5,
            }}
          >
            {previewRoster.map((j) => (
              <JobRow key={j.id} j={j} dense />
            ))}
            {overflowJobs > 0 && (
              <div
                style={{
                  fontSize: 10,
                  color: "var(--text-faint)",
                  padding: "4px 8px 0 12px",
                }}
              >
                + {overflowJobs} more — open detail to view full roster
              </div>
            )}
            {previewRoster.length === 0 && (
              <div
                style={{
                  fontSize: 11,
                  color: "var(--text-faint)",
                  padding: "8px 10px",
                }}
              >
                No jobs in flight.
              </div>
            )}
          </div>
        </BentoCell>
      </div>

      {/* DBs row */}
      <div
        style={{
          padding: "12px 20px 14px",
          borderTop: "1px solid var(--border-weak)",
          display: "flex",
          alignItems: "center",
          gap: 10,
          flexWrap: "wrap",
        }}
      >
        <Eyebrow>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
            <Database size={11} /> Databases ·{" "}
            <span style={{ color: "var(--text-muted)" }}>
              {c.completedToday} done today ·{" "}
            </span>
            <span
              style={{
                color: c.failedToday > 5 ? "var(--warning)" : "inherit",
              }}
            >
              {c.failedToday} failed
            </span>
          </span>
        </Eyebrow>
        <span style={{ flex: 1 }} />
        {c.readyDbs.map((db) => (
          <PremiumChip key={db.name} tone="success">
            <Flame size={10} /> {db.name}
          </PremiumChip>
        ))}
        {c.warmingDbs.map((n) => (
          <PremiumChip key={n} tone="warning">
            warming · {n}
          </PremiumChip>
        ))}
      </div>

      {modalOpen && (
        <ClusterDetailModal
          c={c}
          onClose={() => setModalOpen(false)}
        />
      )}
    </div>
  );
}

function BentoCell({
  children,
  span,
  accent,
}: {
  children: React.ReactNode;
  span?: [number, number];
  accent?: string;
}) {
  return (
    <div
      style={{
        gridColumn: span ? `span ${span[0]}` : undefined,
        gridRow: span ? `span ${span[1]}` : undefined,
        padding: "14px 16px",
        background:
          "linear-gradient(160deg, rgba(255,255,255,0.025) 0%, rgba(255,255,255,0.005) 100%)",
        border: "1px solid var(--border-weak)",
        borderRadius: 12,
        display: "flex",
        flexDirection: "column",
        minHeight: 0,
        position: "relative",
        overflow: "hidden",
      }}
    >
      {accent && (
        <div
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            right: 0,
            height: 1,
            background: `linear-gradient(90deg, transparent 0%, ${accent} 30%, ${accent} 70%, transparent 100%)`,
            opacity: 0.5,
          }}
        />
      )}
      {children}
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Cluster Detail Modal — surfaced from the P3 "Open" button             */
/* -------------------------------------------------------------------- */

function ClusterDetailModal({
  c,
  onClose,
}: {
  c: ClusterTelemetry;
  onClose: () => void;
}) {
  const active = c.jobs.filter(
    (j) => j.state === "Pending" || j.state === "Running" || j.state === "Reducing",
  );
  const recent = c.jobs.filter(
    (j) => j.state === "Completed" || j.state === "Failed",
  );
  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(6,8,12,0.72)",
        backdropFilter: "blur(4px)",
        zIndex: 50,
        display: "flex",
        alignItems: "flex-start",
        justifyContent: "center",
        padding: "32px 24px",
        overflowY: "auto",
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "min(1100px, 100%)",
          background:
            "linear-gradient(180deg, var(--bg-secondary) 0%, var(--bg-primary) 100%)",
          border: "1px solid var(--border-weak)",
          borderRadius: 16,
          boxShadow: "0 24px 60px rgba(0,0,0,0.55)",
          overflow: "hidden",
        }}
      >
        {/* Modal header */}
        <div
          style={{
            padding: "16px 22px",
            display: "flex",
            alignItems: "center",
            gap: 14,
            borderBottom: "1px solid var(--border-weak)",
          }}
        >
          <Server size={16} color="var(--accent)" />
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
              }}
            >
              <strong style={{ fontSize: 16 }}>{c.name}</strong>
              <HealthPill c={c} />
            </div>
            <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
              {c.region} · k8s {c.k8sVersion} · {c.totalNodes} nodes ·{" "}
              {c.pools.map((p) => p.name).join(" + ")}
            </div>
          </div>
          <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
            <ActionBtn tone="neutral">View kubeconfig</ActionBtn>
            <ActionBtn tone="warning">
              <Square size={11} /> Stop cluster
            </ActionBtn>
            <button
              onClick={onClose}
              aria-label="Close"
              style={{
                width: 28,
                height: 28,
                borderRadius: 7,
                border: "1px solid var(--border-medium)",
                background: "transparent",
                color: "var(--text-muted)",
                cursor: "pointer",
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              ×
            </button>
          </div>
        </div>

        {/* Body */}
        <div
          style={{
            padding: "18px 22px 24px",
            display: "flex",
            flexDirection: "column",
            gap: 22,
          }}
        >
          {c.health !== "healthy" && (
            <div
              style={{
                padding: "10px 14px",
                background: "rgba(242,153,74,0.08)",
                border: "1px solid rgba(242,153,74,0.35)",
                borderRadius: 10,
                display: "flex",
                alignItems: "center",
                gap: 10,
                fontSize: 12,
                color: "var(--text-primary)",
              }}
            >
              <AlertTriangle size={14} color="var(--warning)" />
              <strong>Why degraded:</strong>
              <span style={{ color: "var(--text-muted)" }}>
                {c.healthReason}
              </span>
            </div>
          )}

          {/* Top stats row */}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(4, 1fr)",
              gap: 12,
            }}
          >
            <ModalStat label="Submits · 24 h" value={fmtNum(c.submitsLast24h)} />
            <ModalStat
              label="Errors · 15 m"
              value={`${c.submitErrors15m}`}
              tone={
                c.submitErrors15m > 5
                  ? "var(--danger)"
                  : c.submitErrors15m > 0
                    ? "var(--warning)"
                    : "var(--success)"
              }
            />
            <ModalStat
              label="API p95"
              value={fmtMs(c.p95ms)}
              tone={
                c.p95ms > 2000
                  ? "var(--danger)"
                  : c.p95ms > 1000
                    ? "var(--warning)"
                    : "var(--text-primary)"
              }
            />
            <ModalStat
              label="Active jobs"
              value={`${active.length}`}
              hint={`${c.completedToday} done · ${c.failedToday} failed today`}
            />
          </div>

          {/* Active jobs full table */}
          <ModalSection
            icon={<Zap size={12} />}
            title={`Active jobs · ${active.length}`}
            actions={
              <span
                style={{
                  fontSize: 10,
                  color: "var(--text-faint)",
                  letterSpacing: "0.06em",
                  textTransform: "uppercase",
                }}
              >
                live · auto-refresh 5 s
              </span>
            }
          >
            <div
              style={{
                border: "1px solid var(--border-weak)",
                borderRadius: 10,
                overflow: "hidden",
                background: "rgba(255,255,255,0.015)",
              }}
            >
              <ModalJobHeader />
              {active.map((j) => (
                <ModalJobRow key={j.id} j={j} />
              ))}
              {active.length === 0 && (
                <div
                  style={{
                    padding: "16px",
                    fontSize: 11,
                    color: "var(--text-faint)",
                    textAlign: "center",
                  }}
                >
                  No jobs in flight.
                </div>
              )}
            </div>
          </ModalSection>

          {/* Recent jobs */}
          {recent.length > 0 && (
            <ModalSection
              icon={<Clock size={12} />}
              title={`Recently finished · last ${recent.length}`}
            >
              <div
                style={{
                  border: "1px solid var(--border-weak)",
                  borderRadius: 10,
                  overflow: "hidden",
                  background: "rgba(255,255,255,0.015)",
                }}
              >
                <ModalJobHeader />
                {recent.map((j) => (
                  <ModalJobRow key={j.id} j={j} />
                ))}
              </div>
            </ModalSection>
          )}

          {/* Two-column row: pools + databases */}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "1fr 1fr",
              gap: 22,
            }}
          >
            <ModalSection icon={<Server size={12} />} title="Node pools">
              <div
                style={{
                  border: "1px solid var(--border-weak)",
                  borderRadius: 10,
                  overflow: "hidden",
                  background: "rgba(255,255,255,0.015)",
                }}
              >
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "1fr 1.4fr 60px 70px",
                    padding: "8px 12px",
                    fontSize: 10,
                    color: "var(--text-faint)",
                    textTransform: "uppercase",
                    letterSpacing: "0.08em",
                    fontWeight: 600,
                    borderBottom: "1px solid var(--border-weak)",
                  }}
                >
                  <span>Pool</span>
                  <span>SKU</span>
                  <span style={{ textAlign: "right" }}>Nodes</span>
                  <span style={{ textAlign: "right" }}>Role</span>
                </div>
                {c.pools.map((p) => (
                  <div
                    key={p.name}
                    style={{
                      display: "grid",
                      gridTemplateColumns: "1fr 1.4fr 60px 70px",
                      padding: "10px 12px",
                      fontSize: 12,
                      borderBottom: "1px solid var(--border-weak)",
                    }}
                  >
                    <span style={{ color: "var(--text-primary)", fontWeight: 500 }}>
                      {p.name}
                    </span>
                    <span
                      style={{
                        fontFamily: "var(--font-mono)",
                        fontSize: 11,
                        color: "var(--text-muted)",
                      }}
                    >
                      {p.sku}
                    </span>
                    <span
                      style={{
                        textAlign: "right",
                        fontVariantNumeric: "tabular-nums",
                      }}
                    >
                      {p.nodes}
                    </span>
                    <span
                      style={{
                        textAlign: "right",
                        fontSize: 10,
                        color:
                          p.role === "user" ? "var(--accent)" : "var(--text-muted)",
                        textTransform: "uppercase",
                        letterSpacing: "0.06em",
                        fontWeight: 600,
                      }}
                    >
                      {p.role}
                    </span>
                  </div>
                ))}
              </div>
            </ModalSection>

            <ModalSection icon={<Database size={12} />} title="Databases">
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {c.readyDbs.map((db) => (
                  <div
                    key={db.name}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 10,
                      padding: "8px 12px",
                      background: "rgba(115,191,105,0.05)",
                      border: "1px solid rgba(115,191,105,0.18)",
                      borderRadius: 8,
                      fontSize: 12,
                    }}
                  >
                    <Flame size={11} color="var(--success)" />
                    <span
                      style={{
                        flex: 1,
                        fontFamily: "var(--font-mono)",
                        fontSize: 11.5,
                      }}
                    >
                      {db.name}
                    </span>
                    <span
                      style={{
                        fontVariantNumeric: "tabular-nums",
                        color: "var(--text-muted)",
                        fontSize: 11,
                      }}
                    >
                      {db.sizeGb} GiB · ready
                    </span>
                  </div>
                ))}
                {c.warmingDbs.map((n) => (
                  <div
                    key={n}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 10,
                      padding: "8px 12px",
                      background: "rgba(242,153,74,0.05)",
                      border: "1px solid rgba(242,153,74,0.20)",
                      borderRadius: 8,
                      fontSize: 12,
                    }}
                  >
                    <Clock size={11} color="var(--warning)" />
                    <span style={{ flex: 1, fontFamily: "var(--font-mono)", fontSize: 11.5 }}>
                      {n}
                    </span>
                    <span style={{ color: "var(--warning)", fontSize: 11 }}>
                      warming up
                    </span>
                  </div>
                ))}
                {c.unavailableDbs.map((n) => (
                  <div
                    key={n}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 10,
                      padding: "8px 12px",
                      background: "transparent",
                      border: "1px dashed var(--border-weak)",
                      borderRadius: 8,
                      fontSize: 12,
                    }}
                  >
                    <XCircle size={11} color="var(--text-faint)" />
                    <span
                      style={{
                        flex: 1,
                        fontFamily: "var(--font-mono)",
                        fontSize: 11.5,
                        color: "var(--text-faint)",
                      }}
                    >
                      {n}
                    </span>
                    <span style={{ color: "var(--text-faint)", fontSize: 11 }}>
                      not provisioned
                    </span>
                  </div>
                ))}
              </div>
            </ModalSection>
          </div>

          {/* Recent events feed */}
          <ModalSection icon={<Activity size={12} />} title="Recent events">
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 4,
                background: "rgba(255,255,255,0.015)",
                border: "1px solid var(--border-weak)",
                borderRadius: 10,
                padding: 8,
              }}
            >
              {c.events.map((e, i) => (
                <EventLine key={i} e={e} />
              ))}
            </div>
          </ModalSection>
        </div>
      </div>
    </div>
  );
}

function ModalStat({
  label,
  value,
  tone,
  hint,
}: {
  label: string;
  value: string;
  tone?: string;
  hint?: string;
}) {
  return (
    <div
      style={{
        padding: "12px 14px",
        background: "rgba(255,255,255,0.02)",
        border: "1px solid var(--border-weak)",
        borderRadius: 10,
        display: "flex",
        flexDirection: "column",
        gap: 6,
      }}
    >
      <div
        style={{
          fontSize: 10,
          fontWeight: 600,
          color: "var(--text-faint)",
          textTransform: "uppercase",
          letterSpacing: "0.1em",
        }}
      >
        {label}
      </div>
      <div
        style={{
          fontSize: 22,
          fontWeight: 700,
          color: tone ?? "var(--text-primary)",
          fontVariantNumeric: "tabular-nums",
          letterSpacing: "-0.02em",
          lineHeight: 1,
        }}
      >
        {value}
      </div>
      {hint && (
        <div style={{ fontSize: 10.5, color: "var(--text-muted)" }}>{hint}</div>
      )}
    </div>
  );
}

function ModalSection({
  icon,
  title,
  actions,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          marginBottom: 8,
          gap: 8,
        }}
      >
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            fontSize: 11,
            fontWeight: 600,
            color: "var(--text-primary)",
            textTransform: "uppercase",
            letterSpacing: "0.1em",
          }}
        >
          {icon}
          {title}
        </span>
        {actions && <div style={{ marginLeft: "auto" }}>{actions}</div>}
      </div>
      {children}
    </div>
  );
}

function ModalJobHeader() {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "90px 1fr 110px 90px 110px 100px",
        padding: "8px 12px",
        fontSize: 10,
        color: "var(--text-faint)",
        textTransform: "uppercase",
        letterSpacing: "0.08em",
        fontWeight: 600,
        borderBottom: "1px solid var(--border-weak)",
      }}
    >
      <span>Job</span>
      <span>Query · DB</span>
      <span>Splits</span>
      <span>Submitter</span>
      <span style={{ textAlign: "right" }}>Elapsed · ETA</span>
      <span style={{ textAlign: "right" }}>State</span>
    </div>
  );
}

function ModalJobRow({ j }: { j: BlastJob }) {
  const tone =
    j.state === "Failed"
      ? "var(--danger)"
      : j.state === "Pending"
        ? "var(--text-faint)"
        : j.state === "Reducing"
          ? "var(--purple)"
          : j.state === "Completed"
            ? "var(--success)"
            : "var(--accent)";
  // Match JobRow tinting so the modal feels like the same surface.
  const rowBg =
    j.state === "Failed"
      ? "rgba(242,114,111,0.06)"
      : j.state === "Reducing"
        ? "rgba(184,119,217,0.06)"
        : j.state === "Running"
          ? "rgba(110,159,255,0.05)"
          : j.state === "Completed"
            ? "rgba(115,191,105,0.045)"
            : "rgba(255,255,255,0.02)"; // Pending
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "90px 1fr 110px 90px 110px 100px",
        padding: "10px 12px",
        fontSize: 11.5,
        borderBottom: "1px solid var(--border-weak)",
        alignItems: "center",
        background: rowBg,
      }}
    >
      <span
        style={{
          fontFamily: "var(--font-mono)",
          fontSize: 11,
          color: "var(--text-primary)",
          fontWeight: 500,
        }}
      >
        {j.id}
      </span>
      <span
        style={{
          color: "var(--text-muted)",
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        <span
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 11,
            color: "var(--text-primary)",
          }}
        >
          {j.query}
        </span>
        <span style={{ marginLeft: 6 }}>
          · <span style={{ color: "var(--accent)" }}>{j.db}</span>
        </span>
        {j.note && (
          <span style={{ color: tone, marginLeft: 8, fontSize: 11 }}>
            · {j.note}
          </span>
        )}
        {j.hits !== undefined && (
          <span style={{ color: "var(--success)", marginLeft: 8, fontSize: 11 }}>
            · {j.hits.toLocaleString()} hits
          </span>
        )}
      </span>
      <span
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
        }}
      >
        <SplitProgress done={j.splitsDone} total={j.splitsTotal} color={tone} />
        <span
          style={{
            fontSize: 10.5,
            color: "var(--text-muted)",
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {j.splitsDone}/{j.splitsTotal}
        </span>
      </span>
      <span style={{ fontSize: 10.5, color: "var(--text-muted)" }}>
        {j.submitter}
      </span>
      <span
        style={{
          textAlign: "right",
          fontVariantNumeric: "tabular-nums",
          fontSize: 11,
          color: "var(--text-muted)",
        }}
      >
        {j.state === "Pending"
          ? "queued"
          : j.etaSec
            ? `${fmtDuration(j.elapsedSec)} · ETA ${fmtDuration(j.etaSec)}`
            : fmtDuration(j.elapsedSec)}
      </span>
      <span style={{ textAlign: "right" }}>
        <JobStateBadge s={j.state} />
      </span>
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Section header                                                        */
/* -------------------------------------------------------------------- */

function SectionHeader({
  variant,
  title,
  subtitle,
}: {
  variant: string;
  title: string;
  subtitle: string;
}) {
  return (
    <div style={{ marginBottom: 16 }}>
      <div
        style={{
          fontSize: 10,
          fontWeight: 700,
          letterSpacing: "0.14em",
          color: "var(--accent)",
          textTransform: "uppercase",
          marginBottom: 4,
        }}
      >
        {variant}
      </div>
      <div
        style={{ fontSize: 18, fontWeight: 600, letterSpacing: "-0.01em" }}
      >
        {title}
      </div>
      <div style={{ fontSize: 12.5, color: "var(--text-muted)", marginTop: 4 }}>
        {subtitle}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Page                                                                  */
/* -------------------------------------------------------------------- */

export function AksCardMockupsPremium() {
  const [showDegraded, setShowDegraded] = useState(true);
  const visible = showDegraded ? CLUSTERS : [HEALTHY];
  return (
    <div style={{ padding: "32px 24px", maxWidth: 1200, margin: "0 auto" }}>
      <style>
        {`@keyframes elbPulse {
          0%,100% { box-shadow: 0 0 0 0 currentColor; opacity: 1; }
          50%    { box-shadow: 0 0 0 6px transparent; opacity: 0.6; }
        }`}
      </style>
      <div style={{ marginBottom: 24 }}>
        <h1 style={{ fontSize: 24, marginBottom: 6, letterSpacing: "-0.02em" }}>
          AKS card · premium proposals
        </h1>
        <p
          style={{
            fontSize: 13,
            color: "var(--text-muted)",
            marginTop: 0,
            lineHeight: 1.6,
          }}
        >
          Three premium layouts, all on the same telemetry: <em>submit
          volume</em> as the headline, with response time, errors, active
          jobs, CPU and memory visible at the same time. Fixture below
          carries one healthy and one degraded cluster — toggle the
          degraded card to see how each layout behaves under load.
        </p>
        <label
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 8,
            marginTop: 12,
            fontSize: 11,
            color: "var(--text-muted)",
            cursor: "pointer",
          }}
        >
          <input
            type="checkbox"
            checked={showDegraded}
            onChange={(e) => setShowDegraded(e.target.checked)}
          />
          show degraded cluster
        </label>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 44 }}>
        <section>
          <SectionHeader
            variant="Variant P1"
            title="Editorial Spread"
            subtitle="Submit count is sized like a magazine cover headline. The six secondary metrics breathe in a calm 3-column grid below. Inspired by Linear / Vercel analytics — generous whitespace, subtle gradient, refined hierarchy."
          />
          <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
            {visible.map((c) => (
              <VariantP1 key={c.name} c={c} />
            ))}
          </div>
        </section>

        <section>
          <SectionHeader
            variant="Variant P2"
            title="Telemetry Console"
            subtitle="Datadog-style 6-tile grid. The submit tile is doubled in width so it remains the visual centre of mass; everything else is a uniform tile with sparkline + trend. Fastest layout to learn for IT ops."
          />
          <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
            {visible.map((c) => (
              <VariantP2 key={c.name} c={c} />
            ))}
          </div>
        </section>

        <section>
          <SectionHeader
            variant="Variant P3"
            title="Mission Control · Bento"
            subtitle="Bento-grid: a hero submit panel, an always-on live activity feed (key for catching external API failures the moment they appear), plus specialist cells for p95 / errors / CPU / memory. Most operationally rich."
          />
          <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
            {visible.map((c) => (
              <VariantP3 key={c.name} c={c} />
            ))}
          </div>
        </section>
      </div>

      <div
        style={{
          marginTop: 48,
          padding: "18px 22px",
          background:
            "linear-gradient(160deg, rgba(110,159,255,0.06) 0%, rgba(110,159,255,0.01) 100%)",
          border: "1px solid var(--border-weak)",
          borderRadius: 12,
          fontSize: 12.5,
          color: "var(--text-muted)",
          lineHeight: 1.65,
        }}
      >
        <strong style={{ color: "var(--text-primary)", fontSize: 13 }}>
          How they differ in practice
        </strong>
        <ul style={{ marginTop: 10, marginBottom: 0, paddingLeft: 20 }}>
          <li>
            <strong>P1 · Editorial Spread</strong> — the calmest. Best when
            the dashboard is going to be looked at by a researcher and the
            metric tiles are scanned only when something feels off.
          </li>
          <li>
            <strong>P2 · Telemetry Console</strong> — symmetric grid, every
            metric reads at a glance with its own sparkline. Best for an
            IT operator who wants every signal in parallel.
          </li>
          <li>
            <strong>P3 · Mission Control</strong> — densest, with a
            permanent live activity feed. Best when external systems are
            actively driving the API and an operator needs to see the
            very next 503 the moment it lands.
          </li>
        </ul>
      </div>
    </div>
  );
}

export default AksCardMockupsPremium;
