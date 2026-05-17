/**
 * AKS card redesign — refined proposals based on Variant C.
 *
 * Three additional layouts that build on the KPI-strip + progressive-
 * disclosure idea but answer the operational concerns raised after the
 * first round:
 *
 *   * Multi-cluster: there may be more than one cluster per workspace,
 *     so vertical density and a fleet-level summary matter.
 *   * Resource pressure: ElasticBLAST jobs are heavy. Admins must see
 *     when a cluster is saturated (CPU / memory / pending pods).
 *   * External API health: when an external system submits jobs via
 *     /api/blast/* and starts failing or slowing down, the admin needs
 *     to catch it within seconds — not when an end-user complains.
 *
 * This page is a static visual prototype. Numbers and time-series below
 * are illustrative fixtures (two clusters: one healthy, one degraded)
 * so the three layouts can be compared on identical signals.
 */

import { useState } from "react";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Clock,
  Cpu,
  Database,
  Flame,
  HardDrive,
  MemoryStick,
  Server,
  Square,
  TrendingDown,
  TrendingUp,
  XCircle,
  Zap,
} from "lucide-react";

/* -------------------------------------------------------------------- */
/* Fixtures                                                              */
/* -------------------------------------------------------------------- */

interface ClusterFixture {
  name: string;
  region: string;
  k8sVersion: string;
  totalNodes: number;
  pools: { name: string; sku: string; nodes: number; role: "system" | "user" }[];
  readyDbs: { name: string; sizeGb: number }[];
  warmingDbs: string[];
  unavailableDbs: string[];
  activeJobs: number;
  /** Resource pressure (0..1). */
  cpuPct: number;
  memPct: number;
  pendingPods: number;
  /** Submit-API health window (last 15 min). */
  apiSubmitErrors: number;
  apiSubmitTotal: number;
  apiP95Ms: number;
  /** Synthetic sparkline (12 points = last hour, 5-min buckets). */
  sparkP95: number[];
  sparkErrors: number[];
  sparkCpu: number[];
  /** Health verdict + concrete reason. */
  health: "healthy" | "degraded" | "down";
  healthReason: string;
  /** Last events (most recent first). */
  events: { t: string; kind: "ok" | "warn" | "err"; msg: string }[];
}

const HEALTHY: ClusterFixture = {
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
  activeJobs: 2,
  cpuPct: 0.42,
  memPct: 0.38,
  pendingPods: 0,
  apiSubmitErrors: 0,
  apiSubmitTotal: 18,
  apiP95Ms: 220,
  sparkP95: [210, 200, 230, 215, 195, 220, 240, 210, 205, 230, 220, 220],
  sparkErrors: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
  sparkCpu: [0.3, 0.32, 0.35, 0.4, 0.42, 0.38, 0.4, 0.45, 0.42, 0.4, 0.42, 0.42],
  health: "healthy",
  healthReason: "All systems nominal",
  events: [
    { t: "2m ago", kind: "ok", msg: "job blast-7f3a completed (3m12s)" },
    { t: "5m ago", kind: "ok", msg: "job blast-9c01 submitted via API" },
    { t: "8m ago", kind: "ok", msg: "job blast-8e22 completed (1m45s)" },
  ],
};

const DEGRADED: ClusterFixture = {
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
  activeJobs: 8,
  cpuPct: 0.92,
  memPct: 0.88,
  pendingPods: 4,
  apiSubmitErrors: 6,
  apiSubmitTotal: 27,
  apiP95Ms: 4200,
  sparkP95: [240, 260, 280, 310, 380, 520, 980, 1500, 2400, 3100, 3800, 4200],
  sparkErrors: [0, 0, 0, 0, 0, 1, 1, 2, 1, 0, 1, 1],
  sparkCpu: [0.5, 0.55, 0.6, 0.68, 0.75, 0.82, 0.88, 0.9, 0.91, 0.92, 0.93, 0.92],
  health: "degraded",
  healthReason: "API p95 4.2s · 6 submit errors / 15m · 4 pods pending",
  events: [
    { t: "30s ago", kind: "err", msg: "POST /api/blast/submit → 503 (timeout)" },
    { t: "1m ago", kind: "err", msg: "POST /api/blast/submit → 503 (timeout)" },
    { t: "2m ago", kind: "warn", msg: "pod blast-job-12a3 Pending (Unschedulable)" },
    { t: "3m ago", kind: "err", msg: "POST /api/blast/submit → 503 (timeout)" },
    { t: "5m ago", kind: "warn", msg: "node aks-user-2 cpu pressure 92%" },
  ],
};

const CLUSTERS = [HEALTHY, DEGRADED];

/* -------------------------------------------------------------------- */
/* Atoms                                                                 */
/* -------------------------------------------------------------------- */

function Sparkline({
  data,
  width = 80,
  height = 22,
  color,
  fill = true,
}: {
  data: number[];
  width?: number;
  height?: number;
  color: string;
  fill?: boolean;
}) {
  if (data.length === 0) return null;
  const min = Math.min(...data);
  const max = Math.max(...data, min + 1);
  const range = max - min || 1;
  const step = width / (data.length - 1 || 1);
  const pts = data
    .map((v, i) => `${i * step},${height - ((v - min) / range) * height}`)
    .join(" ");
  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      {fill && (
        <polygon
          points={`0,${height} ${pts} ${width},${height}`}
          fill={color}
          fillOpacity={0.15}
        />
      )}
      <polyline
        points={pts}
        fill="none"
        stroke={color}
        strokeWidth={1.3}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}

function Gauge({
  pct,
  label,
  icon,
}: {
  pct: number;
  label: string;
  icon: React.ReactNode;
}) {
  const color =
    pct >= 0.85
      ? "var(--danger)"
      : pct >= 0.7
        ? "var(--warning)"
        : "var(--success)";
  return (
    <div style={{ flex: 1, minWidth: 0 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          fontSize: 11,
          color: "var(--text-muted)",
          marginBottom: 4,
        }}
      >
        {icon}
        <span>{label}</span>
        <span style={{ marginLeft: "auto", color, fontWeight: 600 }}>
          {Math.round(pct * 100)}%
        </span>
      </div>
      <div
        style={{
          height: 6,
          background: "var(--bg-tertiary)",
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
    </div>
  );
}

function HealthBadge({ c }: { c: ClusterFixture }) {
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
  const label = c.health[0].toUpperCase() + c.health.slice(1);
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "3px 10px",
        borderRadius: 999,
        background: `${tone}1f`,
        border: `1px solid ${tone}55`,
        color: tone,
        fontSize: 11,
        fontWeight: 600,
      }}
    >
      <Icon size={11} strokeWidth={2} /> {label}
    </span>
  );
}

function btnStyle(tone: "warning" | "danger"): React.CSSProperties {
  const color = tone === "warning" ? "var(--warning)" : "var(--danger)";
  return {
    display: "inline-flex",
    alignItems: "center",
    gap: 4,
    padding: "3px 9px",
    fontSize: 11,
    color,
    background: "transparent",
    border: "1px solid var(--border-weak)",
    borderRadius: 6,
    cursor: "pointer",
  };
}

function Trend({ delta }: { delta: number }) {
  if (Math.abs(delta) < 0.02) {
    return <span style={{ color: "var(--text-faint)", fontSize: 10 }}>→</span>;
  }
  const up = delta > 0;
  const color = up ? "var(--warning)" : "var(--success)";
  const Icon = up ? TrendingUp : TrendingDown;
  return (
    <span
      style={{
        color,
        display: "inline-flex",
        alignItems: "center",
        gap: 2,
        fontSize: 10,
        fontWeight: 600,
      }}
    >
      <Icon size={10} strokeWidth={2} /> {up ? "+" : ""}
      {Math.round(delta * 100)}%
    </span>
  );
}

function sparkDelta(data: number[]): number {
  if (data.length < 2) return 0;
  const first = data.slice(0, Math.max(1, Math.floor(data.length / 3)));
  const last = data.slice(-Math.max(1, Math.floor(data.length / 3)));
  const avg = (xs: number[]) => xs.reduce((a, b) => a + b, 0) / xs.length;
  const a = avg(first);
  const b = avg(last);
  return a === 0 ? 0 : (b - a) / a;
}

/* -------------------------------------------------------------------- */
/* Variant R1 — "Fleet sentinel + per-cluster sparkline KPIs"            */
/*                                                                       */
/* Page-top fleet ribbon aggregates all clusters into a single verdict.  */
/* Each cluster card keeps the C-style KPI strip but every numeric tile  */
/* now carries a sparkline + trend so degradation is visible the moment  */
/* the trend bends. Inline incident banner appears between KPI strip     */
/* and accordions when the cluster is degraded.                          */
/* -------------------------------------------------------------------- */

function FleetRibbon({ clusters }: { clusters: ClusterFixture[] }) {
  const totalReady = clusters.reduce((s, c) => s + c.readyDbs.length, 0);
  const totalJobs = clusters.reduce((s, c) => s + c.activeJobs, 0);
  const totalErrors = clusters.reduce((s, c) => s + c.apiSubmitErrors, 0);
  const worstP95 = Math.max(...clusters.map((c) => c.apiP95Ms));
  const degraded = clusters.filter((c) => c.health !== "healthy");
  const tone =
    degraded.length === 0
      ? "var(--success)"
      : degraded.some((c) => c.health === "down")
        ? "var(--danger)"
        : "var(--warning)";
  const headline =
    degraded.length === 0
      ? `${clusters.length} clusters · all healthy`
      : `${degraded.length} of ${clusters.length} clusters need attention`;
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 16,
        padding: "12px 18px",
        borderRadius: 10,
        background: `${tone}10`,
        border: `1px solid ${tone}40`,
        marginBottom: 16,
      }}
    >
      <div
        style={{
          width: 10,
          height: 10,
          borderRadius: "50%",
          background: tone,
          boxShadow: `0 0 0 4px ${tone}33`,
        }}
      />
      <div style={{ flex: 1 }}>
        <div style={{ fontSize: 14, fontWeight: 600 }}>{headline}</div>
        <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
          {totalReady} DBs ready · {totalJobs} active jobs · {totalErrors}{" "}
          submit errors / 15m · worst p95 {worstP95.toLocaleString()} ms
        </div>
      </div>
    </div>
  );
}

function KpiTileR1({
  icon,
  label,
  value,
  sub,
  spark,
  accent,
  threshold,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  sub?: string;
  spark?: number[];
  accent: string;
  /** "alert" tints the tile background when something is wrong. */
  threshold?: "ok" | "warn" | "alert";
}) {
  const tint =
    threshold === "alert"
      ? "rgba(242,114,111,0.08)"
      : threshold === "warn"
        ? "rgba(242,153,74,0.08)"
        : "transparent";
  return (
    <div
      style={{
        padding: "12px 14px",
        background: tint,
        display: "flex",
        flexDirection: "column",
        gap: 4,
        position: "relative",
        minWidth: 0,
      }}
    >
      <div
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          right: 0,
          height: 2,
          background: accent,
          opacity: 0.55,
        }}
      />
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        {icon}
        <span
          style={{
            fontSize: 10,
            color: "var(--text-muted)",
            textTransform: "uppercase",
            letterSpacing: "0.08em",
            fontWeight: 600,
          }}
        >
          {label}
        </span>
        {spark && (
          <span style={{ marginLeft: "auto" }}>
            <Trend delta={sparkDelta(spark)} />
          </span>
        )}
      </div>
      <div
        style={{
          fontSize: 20,
          fontWeight: 700,
          letterSpacing: "-0.02em",
          color: "var(--text-primary)",
        }}
      >
        {value}
      </div>
      {spark && (
        <Sparkline data={spark} color={accent} width={100} height={20} />
      )}
      {sub && (
        <div style={{ fontSize: 10, color: "var(--text-faint)" }}>{sub}</div>
      )}
    </div>
  );
}

function IncidentBanner({ c }: { c: ClusterFixture }) {
  if (c.health === "healthy") return null;
  const tone = c.health === "down" ? "var(--danger)" : "var(--warning)";
  return (
    <div
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: 10,
        padding: "10px 14px",
        background: `${tone}12`,
        borderTop: `1px solid ${tone}33`,
        borderBottom: `1px solid ${tone}33`,
        fontSize: 12,
      }}
    >
      <AlertTriangle size={14} color={tone} style={{ marginTop: 1 }} />
      <div style={{ flex: 1 }}>
        <div style={{ fontWeight: 600, color: tone }}>
          External submit pipeline is failing
        </div>
        <div style={{ color: "var(--text-muted)", marginTop: 2 }}>
          {c.healthReason}
        </div>
      </div>
      <button
        style={{
          ...btnStyle("warning"),
          color: tone,
          borderColor: `${tone}55`,
        }}
      >
        Open events
      </button>
    </div>
  );
}

function VariantR1Card({ c }: { c: ClusterFixture }) {
  const [openDb, setOpenDb] = useState(true);
  const [openPools, setOpenPools] = useState(false);
  const submitErrPct = c.apiSubmitTotal
    ? c.apiSubmitErrors / c.apiSubmitTotal
    : 0;
  const apiThreshold =
    c.apiP95Ms > 2000 || submitErrPct > 0.05
      ? "alert"
      : c.apiP95Ms > 1000
        ? "warn"
        : "ok";
  const cpuThreshold =
    c.cpuPct > 0.85 ? "alert" : c.cpuPct > 0.7 ? "warn" : "ok";
  return (
    <div
      style={{
        background: "var(--bg-primary)",
        border: "1px solid var(--border-weak)",
        borderRadius: 12,
        overflow: "hidden",
        boxShadow: "var(--shadow-panel)",
      }}
    >
      {/* Title row */}
      <div
        style={{
          padding: "12px 18px",
          display: "flex",
          alignItems: "center",
          gap: 10,
          borderBottom: "1px solid var(--border-weak)",
        }}
      >
        <Server size={14} color="var(--accent)" />
        <strong style={{ fontSize: 14 }}>{c.name}</strong>
        <HealthBadge c={c} />
        <span style={{ fontSize: 11, color: "var(--text-faint)" }}>
          · {c.region}
        </span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <button style={btnStyle("warning")}>
            <Square size={11} /> Stop
          </button>
        </div>
      </div>

      {/* 5 KPI tiles */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(5, 1fr)",
          gap: 1,
          background: "var(--border-weak)",
        }}
      >
        <KpiTileR1
          icon={<CheckCircle2 size={14} color="var(--accent)" />}
          label="Status"
          value={
            c.health === "healthy"
              ? "Healthy"
              : c.health === "degraded"
                ? "Degraded"
                : "Down"
          }
          sub={c.healthReason.length > 30 ? undefined : c.healthReason}
          accent={
            c.health === "healthy"
              ? "var(--success)"
              : c.health === "degraded"
                ? "var(--warning)"
                : "var(--danger)"
          }
          threshold={
            c.health === "healthy"
              ? "ok"
              : c.health === "down"
                ? "alert"
                : "warn"
          }
        />
        <KpiTileR1
          icon={<Activity size={14} color="var(--accent)" />}
          label="API p95 (15m)"
          value={
            c.apiP95Ms >= 1000
              ? `${(c.apiP95Ms / 1000).toFixed(1)} s`
              : `${c.apiP95Ms} ms`
          }
          sub={`${c.apiSubmitErrors} err / ${c.apiSubmitTotal}`}
          spark={c.sparkP95}
          accent="var(--accent)"
          threshold={apiThreshold}
        />
        <KpiTileR1
          icon={<Cpu size={14} color="var(--teal)" />}
          label="CPU pressure"
          value={`${Math.round(c.cpuPct * 100)}%`}
          sub={`${c.pendingPods} pods pending`}
          spark={c.sparkCpu}
          accent="var(--teal)"
          threshold={cpuThreshold}
        />
        <KpiTileR1
          icon={<Database size={14} color="var(--success)" />}
          label="Ready DBs"
          value={`${c.readyDbs.length} / ${
            c.readyDbs.length + c.warmingDbs.length + c.unavailableDbs.length
          }`}
          accent="var(--success)"
        />
        <KpiTileR1
          icon={<Zap size={14} color="var(--warning)" />}
          label="Active jobs"
          value={`${c.activeJobs}`}
          sub={`${c.totalNodes} nodes · k8s ${c.k8sVersion}`}
          accent="var(--warning)"
        />
      </div>

      <IncidentBanner c={c} />

      {/* Accordions */}
      <AccordionR
        open={openDb}
        onToggle={() => setOpenDb((v) => !v)}
        title="Databases"
        badge={`${c.readyDbs.length} ready · ${c.warmingDbs.length} warming`}
      >
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {c.readyDbs.map((db) => (
            <ChipR key={db.name} tone="success">
              <Flame size={10} /> {db.name}
            </ChipR>
          ))}
          {c.warmingDbs.map((n) => (
            <ChipR key={n} tone="warning">
              warming · {n}
            </ChipR>
          ))}
          {c.unavailableDbs.map((n) => (
            <ChipR key={n} tone="muted">
              {n}
            </ChipR>
          ))}
        </div>
      </AccordionR>
      <AccordionR
        open={openPools}
        onToggle={() => setOpenPools((v) => !v)}
        title="Node pools"
        badge={`${c.pools.length} pools · ${c.totalNodes} nodes`}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
          {c.pools.map((p) => (
            <div
              key={p.name}
              style={{
                display: "flex",
                justifyContent: "space-between",
                padding: "5px 10px",
                background: "var(--bg-secondary)",
                borderRadius: 6,
                fontSize: 12,
              }}
            >
              <span>
                <strong>{p.name}</strong>{" "}
                <span style={{ color: "var(--text-faint)" }}>· {p.role}</span>
              </span>
              <span
                style={{
                  color: "var(--text-muted)",
                  fontFamily: "var(--font-mono)",
                }}
              >
                {p.nodes}× {p.sku}
              </span>
            </div>
          ))}
        </div>
      </AccordionR>
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Variant R2 — "Pressure gauges + live activity column"                 */
/*                                                                       */
/* KPI strip stays for the 3-sec scan. Below it a two-column body:       */
/*  LEFT  → resource gauges (CPU/Mem/Pending pods) + databases           */
/*  RIGHT → live activity column showing the last few events with        */
/*          colored severity, so an API spike is visible the moment a    */
/*          single 503 row appears at the top.                           */
/* -------------------------------------------------------------------- */

function VariantR2Card({ c }: { c: ClusterFixture }) {
  return (
    <div
      style={{
        background: "var(--bg-primary)",
        border: "1px solid var(--border-weak)",
        borderRadius: 12,
        overflow: "hidden",
        boxShadow: "var(--shadow-panel)",
      }}
    >
      {/* Title row + KPI strip */}
      <div
        style={{
          padding: "12px 18px",
          display: "flex",
          alignItems: "center",
          gap: 10,
          borderBottom: "1px solid var(--border-weak)",
        }}
      >
        <Server size={14} color="var(--accent)" />
        <strong style={{ fontSize: 14 }}>{c.name}</strong>
        <HealthBadge c={c} />
        <span style={{ fontSize: 11, color: "var(--text-faint)" }}>
          · {c.region} · k8s {c.k8sVersion}
        </span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <button style={btnStyle("warning")}>
            <Square size={11} /> Stop
          </button>
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr 1fr 1fr",
          gap: 1,
          background: "var(--border-weak)",
        }}
      >
        <MetricCell
          icon={<Activity size={14} color="var(--accent)" />}
          label="API p95"
          value={
            c.apiP95Ms >= 1000
              ? `${(c.apiP95Ms / 1000).toFixed(1)}s`
              : `${c.apiP95Ms}ms`
          }
          accent="var(--accent)"
          spark={c.sparkP95}
        />
        <MetricCell
          icon={<XCircle size={14} color="var(--danger)" />}
          label="Submit errors (15m)"
          value={`${c.apiSubmitErrors} / ${c.apiSubmitTotal}`}
          accent="var(--danger)"
          spark={c.sparkErrors.length ? c.sparkErrors : undefined}
        />
        <MetricCell
          icon={<Zap size={14} color="var(--warning)" />}
          label="Active jobs"
          value={`${c.activeJobs}`}
          accent="var(--warning)"
        />
        <MetricCell
          icon={<Database size={14} color="var(--success)" />}
          label="Ready DBs"
          value={`${c.readyDbs.length}`}
          accent="var(--success)"
        />
      </div>

      {/* Two-column body */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1.3fr 1fr",
          gap: 0,
        }}
      >
        <div style={{ padding: "14px 18px" }}>
          <SectionLabel>Resource pressure</SectionLabel>
          <div style={{ display: "flex", gap: 12, marginBottom: 12 }}>
            <Gauge
              pct={c.cpuPct}
              label="CPU"
              icon={<Cpu size={11} />}
            />
            <Gauge
              pct={c.memPct}
              label="Memory"
              icon={<MemoryStick size={11} />}
            />
            <div
              style={{
                padding: "0 12px",
                borderLeft: "1px solid var(--border-weak)",
                display: "flex",
                flexDirection: "column",
                justifyContent: "center",
                minWidth: 90,
              }}
            >
              <div
                style={{
                  fontSize: 10,
                  color: "var(--text-muted)",
                  letterSpacing: "0.06em",
                }}
              >
                PENDING PODS
              </div>
              <div
                style={{
                  fontSize: 16,
                  fontWeight: 700,
                  color:
                    c.pendingPods > 0 ? "var(--warning)" : "var(--text-primary)",
                }}
              >
                {c.pendingPods}
              </div>
            </div>
          </div>

          <SectionLabel>Databases</SectionLabel>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {c.readyDbs.map((db) => (
              <ChipR key={db.name} tone="success">
                <Flame size={10} /> {db.name}
              </ChipR>
            ))}
            {c.warmingDbs.map((n) => (
              <ChipR key={n} tone="warning">
                warming · {n}
              </ChipR>
            ))}
            {c.unavailableDbs.map((n) => (
              <ChipR key={n} tone="muted">
                {n}
              </ChipR>
            ))}
          </div>
        </div>

        <div
          style={{
            padding: "14px 18px",
            background: "rgba(255,255,255,0.02)",
            borderLeft: "1px solid var(--border-weak)",
          }}
        >
          <SectionLabel>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              <Activity size={11} /> Live activity
            </span>
          </SectionLabel>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {c.events.map((e, i) => (
              <EventRow key={i} event={e} />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function MetricCell({
  icon,
  label,
  value,
  accent,
  spark,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  accent: string;
  spark?: number[];
}) {
  return (
    <div
      style={{
        padding: "10px 14px",
        background: "var(--bg-primary)",
        display: "flex",
        alignItems: "center",
        gap: 10,
      }}
    >
      {icon}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontSize: 10,
            color: "var(--text-muted)",
            textTransform: "uppercase",
            letterSpacing: "0.06em",
          }}
        >
          {label}
        </div>
        <div
          style={{ fontSize: 16, fontWeight: 700, color: "var(--text-primary)" }}
        >
          {value}
        </div>
      </div>
      {spark && spark.some((v) => v > 0) && (
        <Sparkline data={spark} color={accent} width={50} height={18} />
      )}
    </div>
  );
}

function EventRow({
  event,
}: {
  event: { t: string; kind: "ok" | "warn" | "err"; msg: string };
}) {
  const color =
    event.kind === "err"
      ? "var(--danger)"
      : event.kind === "warn"
        ? "var(--warning)"
        : "var(--text-muted)";
  const Icon =
    event.kind === "err" ? XCircle : event.kind === "warn" ? AlertTriangle : CheckCircle2;
  return (
    <div
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: 6,
        padding: "5px 8px",
        background: event.kind === "err" ? `${color}10` : "transparent",
        borderRadius: 4,
        fontSize: 11,
        lineHeight: 1.4,
      }}
    >
      <Icon size={11} color={color} style={{ marginTop: 2, flexShrink: 0 }} />
      <span style={{ flex: 1, color: "var(--text-primary)", wordBreak: "break-word" }}>
        {event.msg}
      </span>
      <span
        style={{
          fontSize: 10,
          color: "var(--text-faint)",
          whiteSpace: "nowrap",
          flexShrink: 0,
        }}
      >
        {event.t}
      </span>
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Variant R3 — "Sticky alert ribbon + collapsible cluster strip"        */
/*                                                                       */
/* Optimised for many clusters. A sticky top alert ribbon only shows up  */
/* when something is wrong; clusters compress to a one-line row by       */
/* default so the admin can scan dozens at once. Click a row to expand.  */
/* The compressed row already shows status + p95 + cpu + jobs + DBs,     */
/* and a tiny sparkline for the API trend.                               */
/* -------------------------------------------------------------------- */

function StickyAlertRibbon({ clusters }: { clusters: ClusterFixture[] }) {
  const incidents = clusters.filter((c) => c.health !== "healthy");
  if (incidents.length === 0) return null;
  const top = incidents[0];
  const tone = top.health === "down" ? "var(--danger)" : "var(--warning)";
  return (
    <div
      style={{
        position: "sticky",
        top: 0,
        zIndex: 5,
        marginBottom: 14,
        padding: "10px 16px",
        background: `${tone}1a`,
        border: `1px solid ${tone}55`,
        borderRadius: 8,
        display: "flex",
        alignItems: "center",
        gap: 12,
        backdropFilter: "blur(8px)",
      }}
    >
      <AlertTriangle size={14} color={tone} />
      <div style={{ flex: 1 }}>
        <span style={{ fontSize: 12, fontWeight: 600, color: tone }}>
          {incidents.length} cluster{incidents.length > 1 ? "s" : ""} need
          attention
        </span>
        <span
          style={{
            fontSize: 11,
            color: "var(--text-muted)",
            marginLeft: 8,
          }}
        >
          · top: <strong style={{ color: "var(--text-primary)" }}>{top.name}</strong>{" "}
          — {top.healthReason}
        </span>
      </div>
      <button
        style={{
          ...btnStyle("warning"),
          color: tone,
          borderColor: `${tone}55`,
        }}
      >
        View incidents ({incidents.length})
      </button>
    </div>
  );
}

function ClusterStrip({ c }: { c: ClusterFixture }) {
  const [open, setOpen] = useState(c.health !== "healthy");
  const tone =
    c.health === "healthy"
      ? "var(--success)"
      : c.health === "degraded"
        ? "var(--warning)"
        : "var(--danger)";
  return (
    <div
      style={{
        background: "var(--bg-primary)",
        border: "1px solid var(--border-weak)",
        borderLeft: `3px solid ${tone}`,
        borderRadius: 8,
        overflow: "hidden",
      }}
    >
      <button
        onClick={() => setOpen((v) => !v)}
        style={{
          width: "100%",
          padding: "10px 14px",
          background: "transparent",
          border: "none",
          display: "grid",
          gridTemplateColumns:
            "16px minmax(160px,1.4fr) 80px 90px 90px 80px 110px 28px",
          alignItems: "center",
          gap: 12,
          cursor: "pointer",
          color: "var(--text-primary)",
        }}
      >
        <StripCellDot tone={tone} />
        <StripCellName c={c} />
        <StripCellMetric
          label="API p95"
          value={
            c.apiP95Ms >= 1000
              ? `${(c.apiP95Ms / 1000).toFixed(1)}s`
              : `${c.apiP95Ms}ms`
          }
          alert={c.apiP95Ms > 2000}
        />
        <StripCellMetric
          label="Errors/15m"
          value={`${c.apiSubmitErrors}/${c.apiSubmitTotal}`}
          alert={c.apiSubmitErrors > 0}
        />
        <StripCellMetric
          label="CPU"
          value={`${Math.round(c.cpuPct * 100)}%`}
          alert={c.cpuPct > 0.85}
        />
        <StripCellMetric label="Jobs" value={`${c.activeJobs}`} />
        <Sparkline
          data={c.sparkP95}
          color={c.apiP95Ms > 2000 ? "var(--danger)" : "var(--accent)"}
          width={100}
          height={20}
        />
        <span style={{ color: "var(--text-faint)", justifySelf: "end" }}>
          {open ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </span>
      </button>

      {open && (
        <div
          style={{
            padding: "10px 18px 14px",
            borderTop: "1px solid var(--border-weak)",
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: 18,
          }}
        >
          <div>
            <SectionLabel>Resource pressure</SectionLabel>
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <Gauge pct={c.cpuPct} label="CPU" icon={<Cpu size={11} />} />
              <Gauge
                pct={c.memPct}
                label="Memory"
                icon={<MemoryStick size={11} />}
              />
            </div>
            <div style={{ marginTop: 10 }}>
              <SectionLabel>Databases</SectionLabel>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                {c.readyDbs.map((db) => (
                  <ChipR key={db.name} tone="success">
                    <Flame size={10} /> {db.name}
                  </ChipR>
                ))}
                {c.warmingDbs.map((n) => (
                  <ChipR key={n} tone="warning">
                    warming · {n}
                  </ChipR>
                ))}
              </div>
            </div>
          </div>
          <div>
            <SectionLabel>
              <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                <Clock size={11} /> Last events
              </span>
            </SectionLabel>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {c.events.slice(0, 4).map((e, i) => (
                <EventRow key={i} event={e} />
              ))}
            </div>
            <div
              style={{
                marginTop: 10,
                fontSize: 11,
                color: "var(--text-muted)",
                display: "flex",
                gap: 14,
              }}
            >
              <span>
                <HardDrive size={10} /> {c.totalNodes} nodes
              </span>
              <span>k8s {c.k8sVersion}</span>
              <span>{c.pools.length} pools</span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function StripCellDot({ tone }: { tone: string }) {
  return (
    <span
      style={{
        width: 8,
        height: 8,
        borderRadius: "50%",
        background: tone,
        display: "inline-block",
      }}
    />
  );
}

function StripCellName({ c }: { c: ClusterFixture }) {
  return (
    <div style={{ minWidth: 0 }}>
      <div
        style={{
          fontSize: 13,
          fontWeight: 600,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {c.name}
      </div>
      <div
        style={{
          fontSize: 10,
          color: "var(--text-faint)",
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {c.region} · {c.readyDbs.length} DBs ready
      </div>
    </div>
  );
}

function StripCellMetric({
  label,
  value,
  alert = false,
}: {
  label: string;
  value: string;
  alert?: boolean;
}) {
  return (
    <div style={{ textAlign: "left" }}>
      <div
        style={{
          fontSize: 9,
          color: "var(--text-faint)",
          textTransform: "uppercase",
          letterSpacing: "0.06em",
        }}
      >
        {label}
      </div>
      <div
        style={{
          fontSize: 13,
          fontWeight: 600,
          color: alert ? "var(--danger)" : "var(--text-primary)",
          fontFamily: "var(--font-mono)",
        }}
      >
        {value}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Shared mini-atoms used by R1/R2/R3                                    */
/* -------------------------------------------------------------------- */

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        fontSize: 10,
        fontWeight: 700,
        color: "var(--text-faint)",
        textTransform: "uppercase",
        letterSpacing: "0.08em",
        marginBottom: 8,
      }}
    >
      {children}
    </div>
  );
}

function ChipR({
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
        gap: 4,
        padding: "2px 8px",
        borderRadius: 999,
        background: tone === "muted" ? "transparent" : `${color}1a`,
        border: tone === "muted" ? "1px dashed var(--border-weak)" : `1px solid ${color}40`,
        color,
        fontSize: 11,
        fontWeight: 500,
      }}
    >
      {children}
    </span>
  );
}

function AccordionR({
  open,
  onToggle,
  title,
  badge,
  children,
}: {
  open: boolean;
  onToggle: () => void;
  title: string;
  badge: string;
  children: React.ReactNode;
}) {
  return (
    <div style={{ borderTop: "1px solid var(--border-weak)" }}>
      <button
        onClick={onToggle}
        style={{
          width: "100%",
          padding: "9px 18px",
          background: "transparent",
          border: "none",
          display: "flex",
          alignItems: "center",
          gap: 10,
          cursor: "pointer",
          color: "var(--text-primary)",
          fontSize: 12,
          fontWeight: 600,
        }}
      >
        <ChevronDown
          size={12}
          style={{
            transform: open ? "rotate(0)" : "rotate(-90deg)",
            transition: "transform 120ms ease-out",
            color: "var(--text-faint)",
          }}
        />
        {title}
        <span style={{ fontSize: 10, color: "var(--text-faint)", fontWeight: 400 }}>
          {badge}
        </span>
      </button>
      {open && <div style={{ padding: "4px 18px 12px" }}>{children}</div>}
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Section heading                                                       */
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
    <div style={{ marginBottom: 14 }}>
      <div
        style={{
          fontSize: 10,
          fontWeight: 700,
          letterSpacing: "0.12em",
          color: "var(--text-faint)",
          textTransform: "uppercase",
          marginBottom: 4,
        }}
      >
        {variant}
      </div>
      <div style={{ fontSize: 16, fontWeight: 600 }}>{title}</div>
      <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
        {subtitle}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Page                                                                  */
/* -------------------------------------------------------------------- */

export function AksCardMockupsRefined() {
  return (
    <div style={{ padding: "32px 24px", maxWidth: 1180, margin: "0 auto" }}>
      <div style={{ marginBottom: 28 }}>
        <h1 style={{ fontSize: 22, marginBottom: 6 }}>
          AKS card refinement — three proposals
        </h1>
        <p style={{ fontSize: 13, color: "var(--text-muted)", marginTop: 0 }}>
          All three build on Variant C (KPI strip + progressive disclosure)
          and add three things: (1) multi-cluster scenario, (2) live
          resource pressure, (3) a clear signal when external systems hit
          API errors or slowdowns. Fixture below: two clusters — one
          healthy, one degraded (API p95 4.2 s, 6 submit errors / 15 m,
          92% CPU, 4 pods pending).
        </p>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 40 }}>
        {/* R1 */}
        <section>
          <SectionHeader
            variant="Variant R1"
            title="Fleet sentinel + per-cluster sparkline KPIs"
            subtitle="Page-top ribbon collapses every cluster into one verdict. Each cluster card grows a 5th KPI (API p95 + errors) and every numeric tile carries a sparkline + trend arrow. When a cluster goes degraded, an inline banner appears between KPI strip and details."
          />
          <FleetRibbon clusters={CLUSTERS} />
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            {CLUSTERS.map((c) => (
              <VariantR1Card key={c.name} c={c} />
            ))}
          </div>
        </section>

        {/* R2 */}
        <section>
          <SectionHeader
            variant="Variant R2"
            title="Pressure gauges + live activity column"
            subtitle="KPI strip on top for the 3-sec scan. Below it: left side gives CPU / Memory gauges + databases; right side is a live activity column. A single 503 row appears at the top of the right column the moment an external POST /api/blast/submit fails."
          />
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            {CLUSTERS.map((c) => (
              <VariantR2Card key={c.name} c={c} />
            ))}
          </div>
        </section>

        {/* R3 */}
        <section>
          <SectionHeader
            variant="Variant R3"
            title="Sticky alert ribbon + compact cluster strips"
            subtitle="Optimised for many clusters. Sticky alert ribbon only shows up when something is wrong. Clusters compress to a one-line strip carrying status dot + p95 + errors + CPU + jobs + sparkline so an admin can scan dozens at once; click a row to expand for gauges, databases, and recent events."
          />
          <StickyAlertRibbon clusters={CLUSTERS} />
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {CLUSTERS.map((c) => (
              <ClusterStrip key={c.name} c={c} />
            ))}
          </div>
        </section>
      </div>

      <div
        style={{
          marginTop: 44,
          padding: "16px 18px",
          background: "var(--bg-secondary)",
          border: "1px solid var(--border-weak)",
          borderRadius: 8,
          fontSize: 12,
          color: "var(--text-muted)",
          lineHeight: 1.6,
        }}
      >
        <strong style={{ color: "var(--text-primary)" }}>How to choose:</strong>
        <ul style={{ marginTop: 8, marginBottom: 0, paddingLeft: 18 }}>
          <li>
            <strong>R1</strong> — least disruptive. Same card shape as C,
            just smarter tiles. Best if we stay at 1–3 clusters per
            workspace and don't want to invent new layouts.
          </li>
          <li>
            <strong>R2</strong> — best <em>causal</em> readout. CPU/Mem
            gauges + activity column let an admin pinpoint{" "}
            <em>why</em> the API is slow (saturation? pending pods? a
            single bad node?) in one glance. Slightly taller per card.
          </li>
          <li>
            <strong>R3</strong> — best for fleet operators. The one-line
            strip means 10+ clusters fit on one screen, and the sticky
            ribbon means the admin learns about a degradation the moment
            it crosses threshold. The trade-off is that researchers see
            less DB context until they expand a row.
          </li>
        </ul>
      </div>
    </div>
  );
}

export default AksCardMockupsRefined;
