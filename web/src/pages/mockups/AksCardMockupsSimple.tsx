/**
 * AKS card redesign — simplification proposals (round 4).
 *
 * Brief from user:
 *   현재 AKS 카드가 너무 복잡해졌다. 한 카드 안에 헤더 + Submit pipeline hero +
 *   Live activity rail + Active jobs + CPU/Mem + API latency + Nodes/Pools +
 *   DB chips 까지 8개 영역이 동시에 보이고, 클러스터가 여러 개면 화면이 빽빽해진다.
 *
 * Design direction:
 *   "What is the one thing this cluster needs right now?" — 평상시에는
 *   초간단(1줄 / 3개 KPI)로 보여주고, 깊이 있는 정보는 클릭/펼침/상세 모달로
 *   미루는 progressive disclosure 안 3가지.
 *
 *   Variant A — Single-Line Pulse
 *     클러스터당 1줄. health dot + name + 상태 텍스트 + 3개 숫자.
 *     낮은 시각 부하, 여러 클러스터를 세로로 빠르게 스캔 가능.
 *
 *   Variant B — 3-Up KPI Card
 *     카드당 큰 숫자 3개(Submits 15m / Active jobs / Pressure)만 노출.
 *     활동/이벤트/DB 칩 모두 제거. 메타 정보는 footer 한 줄.
 *
 *   Variant C — Status-Bar + Focus Panel
 *     상단 1줄 status bar + 그 아래 "지금 가장 중요한 한 가지"만 패널로.
 *     상태가 healthy면 quiet green strip, degraded면 무엇이 잘못됐는지
 *     하나의 actionable 카드.
 *
 * This file is a static visual prototype. Fixture below mirrors the
 * "healthy" + "degraded" pair used by the other mockup pages so all
 * four proposals can be compared on identical signals.
 */

import { useState } from "react";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Cpu,
  Database,
  ExternalLink,
  Flame,
  Loader2,
  MemoryStick,
  Send,
  Server,
  User,
  XCircle,
} from "lucide-react";

/* -------------------------------------------------------------------- */
/* Fixture                                                               */
/* -------------------------------------------------------------------- */

interface JobRow {
  id: string;
  /** Query file or batch identifier. */
  query: string;
  db: string;
  state: "Pending" | "Running" | "Reducing" | "Completed" | "Failed";
  splitsDone: number;
  splitsTotal: number;
  elapsedSec: number;
  /** Missing when Pending or stalled. */
  etaSec?: number;
  /** "external-api" | "researcher@lab" | "scheduler" | ... */
  submitter: string;
  /** Short status note (e.g. "Unschedulable", "merging shards", "OOMKilled"). */
  note?: string;
  /** Present once Completed. */
  hits?: number;
}

interface ClusterFixture {
  name: string;
  region: string;
  k8sVersion: string;
  totalNodes: number;
  /** Submit-pipeline volume in the last 15 minutes (POST /api/blast/submit). */
  submits15m: number;
  submits1h: number;
  /** Number of submit-API errors (5xx / timeouts) in the last 15 minutes. */
  apiErrors15m: number;
  /** p95 latency of POST /api/blast/submit (ms), 15-min window. */
  apiP95Ms: number;
  /** Resource pressure on the hottest user-pool node, 0..1. */
  cpuPct: number;
  memPct: number;
  pendingPods: number;
  activeJobs: number;
  /** Jobs roster — "active" first (Pending/Running/Reducing), then a few
   *  recent terminal jobs. Variant A previews up to JOB_PREVIEW rows. */
  jobs: JobRow[];
  /** Completed today (drives the "+N more" affordance). */
  completedToday: number;
  failedToday: number;
  readyDbCount: number;
  warmingDbCount: number;
  unavailableDbCount: number;
  /** Verdict + the single human-readable reason. */
  health: "healthy" | "degraded" | "down";
  /** One-line explanation for the verdict (visible to the operator). */
  healthReason: string;
  /** What the operator should look at first when health != healthy. */
  topConcern?: {
    kind: "cpu" | "memory" | "api-errors" | "pending-pods" | "db";
    headline: string;
    detail: string;
    actionLabel: string;
  };
}

const HEALTHY: ClusterFixture = {
  name: "elb-cluster-prod",
  region: "koreacentral",
  k8sVersion: "1.34.0",
  totalNodes: 4,
  submits15m: 187,
  submits1h: 742,
  apiErrors15m: 0,
  apiP95Ms: 220,
  cpuPct: 0.42,
  memPct: 0.38,
  pendingPods: 0,
  activeJobs: 2,
  completedToday: 138,
  failedToday: 1,
  jobs: [
    {
      id: "job-7f3a",
      query: "queries-2026-05-16-batch-12.fa",
      db: "nt_prok",
      state: "Running",
      splitsDone: 12,
      splitsTotal: 15,
      elapsedSec: 134,
      etaSec: 32,
      submitter: "external-api",
    },
    {
      id: "job-9c01",
      query: "16s-survey-clinical-2026-05.fa",
      db: "16S_ribosomal_RNA",
      state: "Reducing",
      splitsDone: 4,
      splitsTotal: 4,
      elapsedSec: 48,
      submitter: "external-api",
      note: "merging shard outputs",
    },
    {
      id: "job-8e22",
      query: "researcher-curated-2026-05-15.fa",
      db: "ref_viruses_rep_genomes",
      state: "Completed",
      splitsDone: 3,
      splitsTotal: 3,
      elapsedSec: 192,
      submitter: "researcher@lab",
      hits: 1248,
    },
  ],
  readyDbCount: 3,
  warmingDbCount: 1,
  unavailableDbCount: 0,
  health: "healthy",
  healthReason: "All systems nominal · last error 4h ago",
};

const DEGRADED: ClusterFixture = {
  name: "elb-cluster-lab",
  region: "koreacentral",
  k8sVersion: "1.33.4",
  totalNodes: 3,
  submits15m: 312,
  submits1h: 1_204,
  apiErrors15m: 18,
  apiP95Ms: 4_200,
  cpuPct: 0.92,
  memPct: 0.88,
  pendingPods: 4,
  activeJobs: 8,
  completedToday: 92,
  failedToday: 14,
  jobs: [
    {
      id: "job-12a3",
      query: "external-api-batch-2026-05-16-002.fa",
      db: "nt",
      state: "Pending",
      splitsDone: 0,
      splitsTotal: 20,
      elapsedSec: 0,
      submitter: "external-api",
      note: "Unschedulable · no node has 16 cores free",
    },
    {
      id: "job-7f3a",
      query: "external-api-batch-2026-05-16-001.fa",
      db: "nt",
      state: "Running",
      splitsDone: 8,
      splitsTotal: 20,
      elapsedSec: 320,
      submitter: "external-api",
      note: "stalled · splits 9-11 retry x2",
    },
    {
      id: "job-9c02",
      query: "external-api-batch-2026-05-15-093.fa",
      db: "nt",
      state: "Running",
      splitsDone: 14,
      splitsTotal: 20,
      elapsedSec: 412,
      etaSec: 124,
      submitter: "external-api",
    },
    {
      id: "job-9c01",
      query: "researcher-rerun-2026-05-15.fa",
      db: "nt",
      state: "Running",
      splitsDone: 6,
      splitsTotal: 20,
      elapsedSec: 248,
      etaSec: 320,
      submitter: "researcher@lab",
      note: "slow · cpu pressure 92%",
    },
    {
      id: "job-9b88",
      query: "external-api-batch-2026-05-15-091.fa",
      db: "nt",
      state: "Failed",
      splitsDone: 5,
      splitsTotal: 20,
      elapsedSec: 412,
      submitter: "external-api",
      note: "split-13 OOMKilled · 16Gi limit",
    },
  ],
  readyDbCount: 2,
  warmingDbCount: 0,
  unavailableDbCount: 1,
  health: "degraded",
  healthReason: "API p95 4.2s · 18 errors / 15m · CPU 92% · 4 pods pending",
  topConcern: {
    kind: "cpu",
    headline: "User pool saturated — CPU 92%, 4 pods Unschedulable",
    detail:
      "aks-user-2 is at 92% CPU and aks-user-1 at 88%. Two external-api batches are queued waiting for cores. p95 4.2s and 18 submit errors suggest callers are timing out.",
    actionLabel: "Open pool detail",
  },
};

const CLUSTERS: ClusterFixture[] = [HEALTHY, DEGRADED];

/* -------------------------------------------------------------------- */
/* Atoms                                                                 */
/* -------------------------------------------------------------------- */

function toneFor(h: ClusterFixture["health"]): string {
  return h === "healthy"
    ? "var(--success)"
    : h === "degraded"
      ? "var(--warning)"
      : "var(--danger)";
}

function HealthDot({ h, size = 8 }: { h: ClusterFixture["health"]; size?: number }) {
  const tone = toneFor(h);
  return (
    <span
      style={{
        display: "inline-block",
        width: size,
        height: size,
        borderRadius: "50%",
        background: tone,
        boxShadow: h === "healthy" ? `0 0 6px ${tone}88` : `0 0 8px ${tone}cc`,
        flexShrink: 0,
      }}
    />
  );
}

function HealthPill({ h }: { h: ClusterFixture["health"] }) {
  const tone = toneFor(h);
  const Icon = h === "healthy" ? CheckCircle2 : h === "degraded" ? AlertTriangle : XCircle;
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "3px 9px",
        borderRadius: 999,
        background: `${tone}1a`,
        border: `1px solid ${tone}55`,
        color: tone,
        fontSize: 11,
        fontWeight: 600,
        letterSpacing: "0.02em",
      }}
    >
      <Icon size={11} strokeWidth={2.2} />
      {h[0].toUpperCase() + h.slice(1)}
    </span>
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

function Card({
  children,
  accent,
  padding = 16,
}: {
  children: React.ReactNode;
  accent?: string;
  padding?: number;
}) {
  return (
    <div
      className="glass-card"
      style={{
        padding,
        borderTop: accent ? `2px solid ${accent}` : undefined,
        borderRadius: 14,
      }}
    >
      {children}
    </div>
  );
}

function fmtMs(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}

/* -------------------------------------------------------------------- */
/* Variant A — Single-Line Pulse                                         */
/*                                                                       */
/* 클러스터당 1줄.  health dot + name + 작은 메타 + (Submits 15m /        */
/* Active jobs / Pressure) 3개 숫자 + chevron.  클릭하면 한 단계 펼침.    */
/* "여러 클러스터를 종횡으로 빠르게 스캔" 하는 fleet 뷰.                  */
/* -------------------------------------------------------------------- */

function PulseRow({ c, defaultOpen = false }: { c: ClusterFixture; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const pressure = Math.max(c.cpuPct, c.memPct);
  const pressureTone =
    pressure >= 0.85
      ? "var(--danger)"
      : pressure >= 0.7
        ? "var(--warning)"
        : "var(--text-primary)";
  return (
    <div
      className="glass-card"
      style={{
        padding: 0,
        borderRadius: 12,
        overflow: "hidden",
      }}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        style={{
          width: "100%",
          background: "transparent",
          border: "none",
          padding: "12px 14px",
          display: "grid",
          gridTemplateColumns: "auto 1.6fr auto auto auto 16px",
          alignItems: "center",
          gap: 16,
          cursor: "pointer",
          color: "inherit",
          textAlign: "left",
        }}
      >
        <HealthDot h={c.health} />
        <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
          <span
            style={{
              fontSize: 13,
              fontWeight: 600,
              color: "var(--text-primary)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {c.name}
          </span>
          <span
            style={{
              fontSize: 11,
              color: c.health === "healthy" ? "var(--text-faint)" : toneFor(c.health),
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {c.healthReason}
          </span>
        </div>
        <PulseStat
          label="Submits 15m"
          value={c.submits15m.toLocaleString()}
          icon={<Send size={11} />}
        />
        <PulseStat
          label="Active"
          value={c.activeJobs.toString()}
          icon={<Activity size={11} />}
          tone={c.activeJobs > 5 ? "var(--warning)" : undefined}
        />
        <PulseStat
          label="Pressure"
          value={`${Math.round(pressure * 100)}%`}
          icon={<Flame size={11} />}
          tone={pressureTone}
        />
        {open ? (
          <ChevronDown size={14} color="var(--text-faint)" />
        ) : (
          <ChevronRight size={14} color="var(--text-faint)" />
        )}
      </button>
      {open && (
        <div
          style={{
            borderTop: "1px solid var(--border-weak)",
            background: "var(--bg-tertiary)",
          }}
        >
          {/* Meta row */}
          <div
            style={{
              padding: "12px 14px 10px 14px",
              display: "grid",
              gridTemplateColumns: "repeat(4, 1fr)",
              gap: 14,
            }}
          >
            <PulseMeta label="Region" value={c.region} />
            <PulseMeta label="K8s" value={c.k8sVersion} />
            <PulseMeta label="Nodes" value={c.totalNodes.toString()} />
            <PulseMeta
              label="DBs"
              value={`${c.readyDbCount} ready · ${c.warmingDbCount} warming`}
            />
            <PulseMeta label="CPU peak" value={`${Math.round(c.cpuPct * 100)}%`} />
            <PulseMeta label="Mem peak" value={`${Math.round(c.memPct * 100)}%`} />
            <PulseMeta
              label="API p95"
              value={fmtMs(c.apiP95Ms)}
              tone={
                c.apiP95Ms > 2000
                  ? "var(--danger)"
                  : c.apiP95Ms > 1000
                    ? "var(--warning)"
                    : undefined
              }
            />
            <PulseMeta
              label="Errors 15m"
              value={c.apiErrors15m.toString()}
              tone={c.apiErrors15m > 0 ? "var(--danger)" : undefined}
            />
          </div>

          {/* Jobs section */}
          <JobsSection c={c} />

          {/* Actions */}
          <div
            style={{
              padding: "10px 14px 14px 14px",
              borderTop: "1px solid var(--border-weak)",
              display: "flex",
              gap: 8,
            }}
          >
            <FlatBtn>Open cluster detail</FlatBtn>
            <FlatBtn>View jobs</FlatBtn>
            <FlatBtn>View metrics</FlatBtn>
          </div>
        </div>
      )}
    </div>
  );
}

/* ----- Jobs section (Variant A expand) ------------------------------- */

const JOB_PREVIEW = 4;
const JOB_STATE_ORDER: Record<JobRow["state"], number> = {
  Pending: 0,
  Running: 1,
  Reducing: 2,
  Failed: 3,
  Completed: 4,
};

function JobsSection({ c }: { c: ClusterFixture }) {
  const sorted = [...c.jobs].sort(
    (a, b) => JOB_STATE_ORDER[a.state] - JOB_STATE_ORDER[b.state],
  );
  const preview = sorted.slice(0, JOB_PREVIEW);
  const moreCount = Math.max(c.jobs.length - JOB_PREVIEW, 0);

  return (
    <div
      style={{
        padding: "10px 14px 12px 14px",
        borderTop: "1px solid var(--border-weak)",
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        <Eyebrow>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
            <Activity size={10} />
            Jobs
          </span>
        </Eyebrow>
        <span
          style={{
            fontSize: 10,
            color: "var(--text-faint)",
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {c.activeJobs} active · {c.completedToday} done today
          {c.failedToday > 0 ? (
            <>
              {" · "}
              <span style={{ color: "var(--danger)" }}>
                {c.failedToday} failed
              </span>
            </>
          ) : null}
        </span>
      </div>

      {preview.length === 0 ? (
        <div
          style={{
            fontSize: 11,
            color: "var(--text-faint)",
            padding: "8px 4px",
          }}
        >
          No active jobs.
        </div>
      ) : (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 4,
          }}
        >
          {preview.map((j) => (
            <JobLine key={j.id} job={j} />
          ))}
          {moreCount > 0 && (
            <button
              type="button"
              onClick={(e) => e.stopPropagation()}
              style={{
                marginTop: 2,
                alignSelf: "flex-start",
                background: "transparent",
                border: "none",
                color: "var(--accent)",
                fontSize: 11,
                fontWeight: 500,
                cursor: "pointer",
                padding: "2px 0",
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
              }}
            >
              +{moreCount} more job{moreCount === 1 ? "" : "s"}
              <ChevronRight size={11} />
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function JobLine({ job }: { job: JobRow }) {
  const tone = jobStateTone(job.state);
  const pct = job.splitsTotal === 0 ? 0 : job.splitsDone / job.splitsTotal;
  const noteTone = job.note?.includes("OOMKilled") || job.note?.includes("Unschedulable")
    ? "var(--danger)"
    : job.note?.startsWith("slow") || job.note?.startsWith("stalled")
      ? "var(--warning)"
      : "var(--text-faint)";
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "84px minmax(0, 1fr) 110px 96px 120px",
        alignItems: "center",
        gap: 10,
        padding: "6px 8px",
        borderRadius: 6,
        background: "var(--bg-secondary)",
        border: "1px solid var(--border-weak)",
      }}
      title={job.note ?? `${job.state} · ${job.id}`}
    >
      <JobStatePill state={job.state} />
      <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
        <span
          style={{
            fontSize: 12,
            color: "var(--text-primary)",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            fontFamily: "var(--font-mono)",
          }}
        >
          {job.query}
        </span>
        {job.note && (
          <span
            style={{
              fontSize: 10,
              color: noteTone,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {job.note}
          </span>
        )}
      </div>
      <DbChip name={job.db} />
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: 3,
        }}
        title={`${job.splitsDone}/${job.splitsTotal} splits`}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            fontSize: 10,
            color: "var(--text-faint)",
            fontVariantNumeric: "tabular-nums",
          }}
        >
          <span>{job.splitsDone}/{job.splitsTotal}</span>
          <span>{Math.round(pct * 100)}%</span>
        </div>
        <div
          style={{
            height: 4,
            borderRadius: 2,
            background: "var(--bg-canvas)",
            overflow: "hidden",
          }}
        >
          <div
            style={{
              width: `${Math.max(2, pct * 100)}%`,
              height: "100%",
              background: tone,
              transition: "width 200ms ease-out",
            }}
          />
        </div>
      </div>
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "flex-end",
          gap: 2,
          fontVariantNumeric: "tabular-nums",
        }}
      >
        <span style={{ fontSize: 11, color: "var(--text-primary)" }}>
          {jobTimeText(job)}
        </span>
        <span
          style={{
            fontSize: 10,
            color: "var(--text-faint)",
            display: "inline-flex",
            alignItems: "center",
            gap: 3,
          }}
        >
          {job.submitter === "external-api" ? (
            <>
              <ExternalLink size={9} /> external-api
            </>
          ) : (
            <>
              <User size={9} /> {job.submitter}
            </>
          )}
        </span>
      </div>
    </div>
  );
}

function jobStateTone(state: JobRow["state"]): string {
  switch (state) {
    case "Running":
      return "var(--accent)";
    case "Reducing":
      return "var(--teal)";
    case "Completed":
      return "var(--success)";
    case "Pending":
      return "var(--text-faint)";
    case "Failed":
      return "var(--danger)";
  }
}

function JobStatePill({ state }: { state: JobRow["state"] }) {
  const tone = jobStateTone(state);
  const Icon =
    state === "Running"
      ? Loader2
      : state === "Reducing"
        ? Loader2
        : state === "Completed"
          ? CheckCircle2
          : state === "Failed"
            ? XCircle
            : Server;
  const spinning = state === "Running" || state === "Reducing";
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding: "2px 7px",
        borderRadius: 4,
        background: `${tone}1a`,
        border: `1px solid ${tone}44`,
        color: tone,
        fontSize: 10,
        fontWeight: 600,
        letterSpacing: "0.02em",
        textTransform: "uppercase",
        justifyContent: "center",
      }}
    >
      <Icon
        size={10}
        strokeWidth={2.2}
        style={spinning ? { animation: "spin 1.2s linear infinite" } : undefined}
      />
      {state}
    </span>
  );
}

function DbChip({ name }: { name: string }) {
  return (
    <span
      title={name}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding: "2px 8px",
        borderRadius: 4,
        background: "var(--bg-canvas)",
        border: "1px solid var(--border-weak)",
        color: "var(--text-muted)",
        fontSize: 10,
        fontFamily: "var(--font-mono)",
        overflow: "hidden",
        textOverflow: "ellipsis",
        whiteSpace: "nowrap",
        justifyContent: "center",
      }}
    >
      <Database size={9} />
      {name}
    </span>
  );
}

function jobTimeText(j: JobRow): string {
  if (j.state === "Completed") {
    return `done in ${fmtSec(j.elapsedSec)}${j.hits ? ` · ${j.hits.toLocaleString()} hits` : ""}`;
  }
  if (j.state === "Failed") {
    return `failed @ ${fmtSec(j.elapsedSec)}`;
  }
  if (j.state === "Pending") {
    return "queued";
  }
  if (j.etaSec != null) {
    return `${fmtSec(j.elapsedSec)} · ETA ${fmtSec(j.etaSec)}`;
  }
  return `${fmtSec(j.elapsedSec)} · ETA —`;
}

function fmtSec(sec: number): string {
  if (sec <= 0) return "0s";
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m < 60) return s === 0 ? `${m}m` : `${m}m${s}s`;
  const h = Math.floor(m / 60);
  return `${h}h${(m % 60).toString().padStart(2, "0")}m`;
}

function PulseStat({
  label,
  value,
  icon,
  tone,
}: {
  label: string;
  value: string;
  icon: React.ReactNode;
  tone?: string;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 1, minWidth: 80 }}>
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 4,
          fontSize: 10,
          color: "var(--text-faint)",
          textTransform: "uppercase",
          letterSpacing: "0.08em",
        }}
      >
        {icon}
        {label}
      </span>
      <span
        style={{
          fontSize: 16,
          fontWeight: 600,
          color: tone ?? "var(--text-primary)",
          fontVariantNumeric: "tabular-nums",
          lineHeight: 1.1,
        }}
      >
        {value}
      </span>
    </div>
  );
}

function PulseMeta({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <span style={{ fontSize: 10, color: "var(--text-faint)", textTransform: "uppercase", letterSpacing: "0.08em" }}>
        {label}
      </span>
      <span
        style={{
          fontSize: 12,
          fontWeight: 500,
          color: tone ?? "var(--text-primary)",
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {value}
      </span>
    </div>
  );
}

function FlatBtn({ children }: { children: React.ReactNode }) {
  return (
    <button
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "5px 10px",
        fontSize: 11,
        fontWeight: 500,
        color: "var(--text-muted)",
        background: "transparent",
        border: "1px solid var(--border-medium)",
        borderRadius: 7,
        cursor: "pointer",
      }}
    >
      {children}
    </button>
  );
}

/* -------------------------------------------------------------------- */
/* Variant B — 3-Up KPI Card                                             */
/*                                                                       */
/* 카드당 큰 숫자 3개만.  Submits 15m / Active jobs / Pressure.           */
/* 헤더에 이름·health pill·작은 액션, 푸터에 메타 한 줄.                  */
/* 다른 모든 정보(이벤트, DB 칩, latency 등)는 "View details" 모달로.     */
/* -------------------------------------------------------------------- */

function KpiCard({ c }: { c: ClusterFixture }) {
  const pressure = Math.max(c.cpuPct, c.memPct);
  const pressureKind = c.cpuPct >= c.memPct ? "CPU" : "Memory";
  const pressureTone =
    pressure >= 0.85
      ? "var(--danger)"
      : pressure >= 0.7
        ? "var(--warning)"
        : "var(--success)";
  return (
    <Card accent={toneFor(c.health)} padding={18}>
      {/* Header */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          gap: 12,
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 4, minWidth: 0 }}>
          <span
            style={{
              fontSize: 14,
              fontWeight: 600,
              color: "var(--text-primary)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {c.name}
          </span>
          <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
            {c.region} · K8s {c.k8sVersion}
          </span>
        </div>
        <HealthPill h={c.health} />
      </div>

      {/* 3 KPI */}
      <div
        style={{
          marginTop: 20,
          display: "grid",
          gridTemplateColumns: "repeat(3, 1fr)",
          gap: 8,
        }}
      >
        <KpiBlock
          icon={<Send size={12} />}
          label="Submits · 15m"
          value={c.submits15m.toLocaleString()}
          subtle={`1h ${c.submits1h.toLocaleString()}`}
        />
        <KpiBlock
          icon={<Activity size={12} />}
          label="Active jobs"
          value={c.activeJobs.toString()}
          subtle={c.pendingPods > 0 ? `${c.pendingPods} pending` : "none queued"}
          tone={c.pendingPods > 0 ? "var(--warning)" : undefined}
        />
        <KpiBlock
          icon={<Flame size={12} />}
          label={`${pressureKind} peak`}
          value={`${Math.round(pressure * 100)}%`}
          subtle={`${c.totalNodes} nodes`}
          tone={pressureTone}
        />
      </div>

      {/* Single-line reason */}
      <div
        style={{
          marginTop: 14,
          padding: "8px 10px",
          borderRadius: 8,
          background:
            c.health === "healthy"
              ? "rgba(106, 214, 163, 0.06)"
              : c.health === "degraded"
                ? "rgba(240, 198, 116, 0.08)"
                : "rgba(224, 123, 138, 0.08)",
          border: `1px solid ${toneFor(c.health)}33`,
          fontSize: 11,
          color: c.health === "healthy" ? "var(--text-muted)" : toneFor(c.health),
          lineHeight: 1.45,
        }}
      >
        {c.healthReason}
      </div>

      {/* Footer */}
      <div
        style={{
          marginTop: 14,
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          fontSize: 11,
          color: "var(--text-faint)",
        }}
      >
        <span>
          {c.readyDbCount} DBs ready
          {c.warmingDbCount > 0 ? ` · ${c.warmingDbCount} warming` : ""}
          {c.unavailableDbCount > 0 ? ` · ${c.unavailableDbCount} missing` : ""}
        </span>
        <button
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 4,
            background: "transparent",
            border: "none",
            color: "var(--accent)",
            fontSize: 11,
            fontWeight: 500,
            cursor: "pointer",
            padding: 0,
          }}
        >
          View details <ChevronRight size={12} />
        </button>
      </div>
    </Card>
  );
}

function KpiBlock({
  icon,
  label,
  value,
  subtle,
  tone,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  subtle: string;
  tone?: string;
}) {
  return (
    <div
      style={{
        padding: "10px 12px",
        borderRadius: 10,
        background: "var(--bg-tertiary)",
        border: "1px solid var(--border-weak)",
        display: "flex",
        flexDirection: "column",
        gap: 4,
      }}
    >
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 5,
          fontSize: 10,
          color: "var(--text-faint)",
          textTransform: "uppercase",
          letterSpacing: "0.08em",
          fontWeight: 600,
        }}
      >
        {icon}
        {label}
      </span>
      <span
        style={{
          fontSize: 26,
          fontWeight: 600,
          letterSpacing: "-0.01em",
          color: tone ?? "var(--text-primary)",
          fontVariantNumeric: "tabular-nums",
          lineHeight: 1.1,
        }}
      >
        {value}
      </span>
      <span style={{ fontSize: 10, color: "var(--text-faint)" }}>{subtle}</span>
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Variant C — Status-Bar + Focus Panel                                  */
/*                                                                       */
/* 위쪽 1줄 status bar (dot · 이름 · 한 줄 요약 · 핵심 숫자 묶음).        */
/* 아래쪽 단 하나의 "지금 봐야 할 것" 패널.                               */
/*   - healthy: quiet green "All clear" strip                            */
/*   - degraded: orange/red actionable panel with 1 headline + 1 detail   */
/* -------------------------------------------------------------------- */

function FocusCard({ c }: { c: ClusterFixture }) {
  const inlineNums = [
    { icon: <Send size={11} />, label: "Submits 15m", value: c.submits15m.toLocaleString() },
    { icon: <Activity size={11} />, label: "Active", value: c.activeJobs.toString() },
    { icon: <Cpu size={11} />, label: "CPU", value: `${Math.round(c.cpuPct * 100)}%` },
    { icon: <MemoryStick size={11} />, label: "Mem", value: `${Math.round(c.memPct * 100)}%` },
    { icon: <Database size={11} />, label: "DBs", value: `${c.readyDbCount}/${c.readyDbCount + c.warmingDbCount + c.unavailableDbCount}` },
  ];

  return (
    <Card padding={0}>
      {/* Status bar */}
      <div
        style={{
          padding: "12px 16px",
          display: "flex",
          alignItems: "center",
          gap: 14,
          borderBottom: "1px solid var(--border-weak)",
        }}
      >
        <HealthDot h={c.health} size={9} />
        <div style={{ display: "flex", flexDirection: "column", gap: 1, minWidth: 0, flex: 1 }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>
            {c.name}
            <span style={{ color: "var(--text-faint)", fontWeight: 400, marginLeft: 8 }}>
              · {c.region} · {c.totalNodes} nodes
            </span>
          </span>
        </div>
        <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
          {inlineNums.map((n) => (
            <InlineNum key={n.label} {...n} />
          ))}
        </div>
      </div>

      {/* Focus panel */}
      {c.health === "healthy" ? (
        <div
          style={{
            padding: "14px 16px",
            display: "flex",
            alignItems: "center",
            gap: 12,
            background: "rgba(106, 214, 163, 0.05)",
          }}
        >
          <CheckCircle2 size={18} color="var(--success)" />
          <div style={{ display: "flex", flexDirection: "column", gap: 2, flex: 1 }}>
            <span style={{ fontSize: 13, color: "var(--text-primary)", fontWeight: 500 }}>
              All clear
            </span>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
              {c.healthReason}
            </span>
          </div>
          <button
            style={{
              background: "transparent",
              border: "1px solid var(--border-medium)",
              color: "var(--text-muted)",
              borderRadius: 7,
              padding: "5px 10px",
              fontSize: 11,
              cursor: "pointer",
            }}
          >
            View full metrics
          </button>
        </div>
      ) : c.topConcern ? (
        <FocusPanel c={c} concern={c.topConcern} />
      ) : null}
    </Card>
  );
}

function InlineNum({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "baseline",
        gap: 5,
        fontVariantNumeric: "tabular-nums",
      }}
      title={label}
    >
      <span style={{ color: "var(--text-faint)", display: "inline-flex" }}>{icon}</span>
      <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>
        {value}
      </span>
      <span style={{ fontSize: 10, color: "var(--text-faint)" }}>{label}</span>
    </div>
  );
}

function FocusPanel({
  c,
  concern,
}: {
  c: ClusterFixture;
  concern: NonNullable<ClusterFixture["topConcern"]>;
}) {
  const tone = toneFor(c.health);
  const bg =
    c.health === "degraded"
      ? "rgba(240, 198, 116, 0.08)"
      : "rgba(224, 123, 138, 0.08)";
  const Icon =
    concern.kind === "cpu" || concern.kind === "memory"
      ? Flame
      : concern.kind === "api-errors"
        ? AlertTriangle
        : concern.kind === "pending-pods"
          ? Server
          : Database;
  return (
    <div
      style={{
        padding: "14px 16px",
        display: "flex",
        gap: 12,
        background: bg,
      }}
    >
      <div
        style={{
          width: 30,
          height: 30,
          borderRadius: 8,
          background: `${tone}22`,
          border: `1px solid ${tone}55`,
          display: "grid",
          placeItems: "center",
          flexShrink: 0,
        }}
      >
        <Icon size={15} color={tone} />
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4, flex: 1 }}>
        <span style={{ fontSize: 13, color: tone, fontWeight: 600 }}>
          {concern.headline}
        </span>
        <span style={{ fontSize: 11, color: "var(--text-muted)", lineHeight: 1.5 }}>
          {concern.detail}
        </span>
        <div style={{ display: "flex", gap: 8, marginTop: 6 }}>
          <button
            style={{
              background: `${tone}1a`,
              border: `1px solid ${tone}66`,
              color: tone,
              borderRadius: 7,
              padding: "5px 11px",
              fontSize: 11,
              fontWeight: 600,
              cursor: "pointer",
              display: "inline-flex",
              alignItems: "center",
              gap: 5,
            }}
          >
            {concern.actionLabel}
            <ExternalLink size={11} />
          </button>
          <button
            style={{
              background: "transparent",
              border: "1px solid var(--border-medium)",
              color: "var(--text-muted)",
              borderRadius: 7,
              padding: "5px 11px",
              fontSize: 11,
              cursor: "pointer",
            }}
          >
            View full metrics
          </button>
        </div>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Page                                                                  */
/* -------------------------------------------------------------------- */

export function AksCardMockupsSimple() {
  return (
    <div
      style={{
        maxWidth: 1200,
        margin: "0 auto",
        padding: "32px 24px 64px 24px",
        display: "flex",
        flexDirection: "column",
        gap: 40,
      }}
    >
      <header style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <h1 style={{ margin: 0, fontSize: 24, fontWeight: 600, color: "var(--text-primary)" }}>
          AKS card — simplification proposals
        </h1>
        <p style={{ margin: 0, fontSize: 13, color: "var(--text-muted)", lineHeight: 1.6, maxWidth: 720 }}>
          Three progressively-disclosed layouts. The shared fixture is one healthy cluster
          (<code style={{ fontSize: 12 }}>elb-cluster-prod</code>) and one degraded cluster
          (<code style={{ fontSize: 12 }}>elb-cluster-lab</code>) so each variant can be
          compared on identical signals.
        </p>
      </header>

      {/* Variant A */}
      <section style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <VariantHeader
          tag="A"
          title="Single-Line Pulse"
          subtitle="One row per cluster. Health dot · name · status text · 3 numbers · click to expand. Optimised for scanning many clusters at once."
        />
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {CLUSTERS.map((c) => (
            <PulseRow key={c.name} c={c} defaultOpen={c.health !== "healthy"} />
          ))}
        </div>
      </section>

      {/* Variant B */}
      <section style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <VariantHeader
          tag="B"
          title="3-Up KPI Card"
          subtitle="One card per cluster with exactly three big numbers: Submits / Active jobs / Pressure. Everything else moves into a single 'View details' modal."
        />
        <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 16 }}>
          {CLUSTERS.map((c) => (
            <KpiCard key={c.name} c={c} />
          ))}
        </div>
      </section>

      {/* Variant C */}
      <section style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <VariantHeader
          tag="C"
          title="Status-Bar + Focus Panel"
          subtitle="A single status bar (numbers inline) plus exactly one focus panel that answers: 'what should I look at right now?' Healthy shows a quiet green strip; degraded surfaces one actionable concern."
        />
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {CLUSTERS.map((c) => (
            <FocusCard key={c.name} c={c} />
          ))}
        </div>
      </section>

      <footer
        style={{
          marginTop: 16,
          padding: 16,
          borderRadius: 10,
          background: "var(--bg-tertiary)",
          border: "1px solid var(--border-weak)",
          fontSize: 12,
          color: "var(--text-muted)",
          lineHeight: 1.6,
        }}
      >
        <strong style={{ color: "var(--text-primary)" }}>Notes —</strong> 모든 시안은 정적
        프로토타입입니다. 동일한 fixture(healthy / degraded 두 클러스터)를 사용해 비교 가능합니다.
        선택된 시안의 production wiring은 기존 <code>ClusterBento</code>의 data source(
        <code>useNodeSummary</code>, <code>blastApi.listJobs</code>,{" "}
        <code>monitoringApi.requestMetrics</code>, <code>monitoringApi.aksEvents</code>)를 그대로
        재사용합니다.
      </footer>
    </div>
  );
}

function VariantHeader({
  tag,
  title,
  subtitle,
}: {
  tag: string;
  title: string;
  subtitle: string;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span
          style={{
            display: "inline-grid",
            placeItems: "center",
            width: 22,
            height: 22,
            borderRadius: 6,
            background: "var(--accent-soft, rgba(122, 167, 255, 0.18))",
            color: "var(--accent)",
            fontSize: 11,
            fontWeight: 700,
          }}
        >
          {tag}
        </span>
        <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600, color: "var(--text-primary)" }}>
          {title}
        </h2>
      </div>
      <p style={{ margin: 0, fontSize: 12, color: "var(--text-muted)", lineHeight: 1.6, maxWidth: 760 }}>
        {subtitle}
      </p>
    </div>
  );
}

export default AksCardMockupsSimple;
