/**
 * AKS card redesign mockups — three side-by-side proposals.
 *
 * Goal: make the AKS cluster card legible to two audiences without
 * either feeling overwhelmed:
 *   1. Molecular-diagnostics researcher — must perceive AKS as the
 *      "ElasticBLAST execution environment" and know which databases
 *      are warmed and ready to query.
 *   2. IT operations — must grasp cluster health (running / degraded /
 *      stopped, capacity, version drift) in under three seconds.
 *
 * This page is a static visual prototype. No real API calls are made;
 * all numbers and statuses below are illustrative fixtures so the three
 * layouts can be compared on identical data.
 */

import { useState } from "react";
import {
  Activity,
  CheckCircle2,
  ChevronDown,
  Cpu,
  Database,
  Flame,
  HardDrive,
  Server,
  Square,
  Trash2,
  Zap,
} from "lucide-react";

/* -------------------------------------------------------------------- */
/* Shared fixture — identical input to every variant so the comparison  */
/* is about layout, not data.                                           */
/* -------------------------------------------------------------------- */

interface MockCluster {
  name: string;
  region: string;
  k8sVersion: string;
  powerState: "Running" | "Stopped";
  provisioningState: "Succeeded" | "Updating" | "Failed";
  totalNodes: number;
  pools: { name: string; sku: string; nodes: number; role: "system" | "user" }[];
  readyDbs: { name: string; sizeGb: number }[];
  warmingDbs: string[];
  unavailableDbs: string[];
  activeJobs: number;
}

const MOCK: MockCluster = {
  name: "elb-cluster",
  region: "koreacentral",
  k8sVersion: "1.34.0",
  powerState: "Running",
  provisioningState: "Succeeded",
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
};

const PERSONA_COPY = {
  researcher: "Molecular-diagnostics researcher",
  it: "IT operations",
};

/* -------------------------------------------------------------------- */
/* Tiny shared atoms                                                    */
/* -------------------------------------------------------------------- */

function StatusDot({
  color,
  size = 10,
  pulse = false,
}: {
  color: string;
  size?: number;
  pulse?: boolean;
}) {
  return (
    <span
      style={{
        width: size,
        height: size,
        borderRadius: "50%",
        background: color,
        display: "inline-block",
        boxShadow: pulse ? `0 0 0 4px ${color}22` : undefined,
        animation: pulse ? "elbPulse 1.6s ease-in-out infinite" : undefined,
        flexShrink: 0,
      }}
    />
  );
}

function Chip({
  children,
  tone = "neutral",
  size = "md",
}: {
  children: React.ReactNode;
  tone?: "neutral" | "success" | "warning" | "danger" | "accent";
  size?: "sm" | "md";
}) {
  const toneColor =
    tone === "success"
      ? "var(--success)"
      : tone === "warning"
        ? "var(--warning)"
        : tone === "danger"
          ? "var(--danger)"
          : tone === "accent"
            ? "var(--accent)"
            : "var(--text-muted)";
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: size === "sm" ? "2px 8px" : "4px 10px",
        borderRadius: 999,
        background: `${toneColor}1a`,
        border: `1px solid ${toneColor}44`,
        color: toneColor,
        fontSize: size === "sm" ? 11 : 12,
        fontWeight: 500,
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </span>
  );
}

function SectionHeading({
  persona,
  variant,
  title,
  subtitle,
}: {
  persona: string;
  variant: string;
  title: string;
  subtitle: string;
}) {
  return (
    <div style={{ marginBottom: 16 }}>
      <div
        style={{
          fontSize: 10,
          fontWeight: 600,
          letterSpacing: "0.12em",
          color: "var(--text-faint)",
          textTransform: "uppercase",
          marginBottom: 4,
        }}
      >
        {variant} · for {persona}
      </div>
      <div style={{ fontSize: 16, fontWeight: 600, color: "var(--text-primary)" }}>
        {title}
      </div>
      <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
        {subtitle}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Variant A — "Traffic-light hero"                                     */
/* One dominant verdict at the top + a single DB strip + a tiny ops     */
/* footer. The researcher reads only the first two lines; IT scans the  */
/* footer for the version / node count / pool layout.                   */
/* -------------------------------------------------------------------- */

function VariantA() {
  const verdictColor = "var(--success)";
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
      {/* Hero band */}
      <div
        style={{
          padding: "18px 20px",
          display: "flex",
          alignItems: "center",
          gap: 16,
          background:
            "linear-gradient(135deg, rgba(115,191,105,0.10) 0%, rgba(115,191,105,0.02) 60%)",
          borderBottom: "1px solid var(--border-weak)",
        }}
      >
        <StatusDot color={verdictColor} size={18} pulse />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div
            style={{
              fontSize: 18,
              fontWeight: 700,
              color: "var(--text-primary)",
              letterSpacing: "-0.01em",
            }}
          >
            BLAST execution environment is ready
          </div>
          <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 4 }}>
            {MOCK.readyDbs.length} databases warmed · {MOCK.activeJobs} active
            jobs · cluster <code style={{ color: "var(--text-primary)" }}>{MOCK.name}</code>
          </div>
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          <button
            style={btnStyle("warning")}
            title="Stop cluster (saves cost)"
          >
            <Square size={11} /> Stop
          </button>
          <button style={btnStyle("danger")} title="Delete cluster">
            <Trash2 size={11} />
          </button>
        </div>
      </div>

      {/* Database strip — the researcher's primary focus */}
      <div style={{ padding: "16px 20px" }}>
        <div
          style={{
            fontSize: 11,
            fontWeight: 600,
            color: "var(--text-muted)",
            textTransform: "uppercase",
            letterSpacing: "0.08em",
            marginBottom: 10,
            display: "flex",
            alignItems: "center",
            gap: 6,
          }}
        >
          <Database size={12} /> Available databases
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {MOCK.readyDbs.map((db) => (
            <Chip key={db.name} tone="success">
              <Flame size={11} strokeWidth={2} /> {db.name}{" "}
              <span style={{ opacity: 0.6, fontWeight: 400 }}>
                {db.sizeGb} GB
              </span>
            </Chip>
          ))}
          {MOCK.warmingDbs.map((name) => (
            <Chip key={name} tone="warning">
              warming · {name}
            </Chip>
          ))}
          {MOCK.unavailableDbs.map((name) => (
            <Chip key={name} tone="neutral" size="sm">
              {name} (not loaded)
            </Chip>
          ))}
        </div>
      </div>

      {/* IT footer — small but always visible */}
      <div
        style={{
          padding: "10px 20px",
          background: "var(--bg-secondary)",
          borderTop: "1px solid var(--border-weak)",
          display: "flex",
          gap: 18,
          fontSize: 11,
          color: "var(--text-muted)",
          flexWrap: "wrap",
        }}
      >
        <FooterStat icon={<Server size={11} />} label={`${MOCK.totalNodes} nodes`} />
        <FooterStat
          icon={<Cpu size={11} />}
          label={MOCK.pools.map((p) => `${p.name}:${p.sku}`).join(" · ")}
        />
        <FooterStat icon={<Activity size={11} />} label={`k8s ${MOCK.k8sVersion}`} />
        <FooterStat
          icon={<CheckCircle2 size={11} color="var(--success)" />}
          label={MOCK.provisioningState}
        />
        <span style={{ marginLeft: "auto", color: "var(--text-faint)" }}>
          {MOCK.region}
        </span>
      </div>
    </div>
  );
}

function FooterStat({
  icon,
  label,
}: {
  icon: React.ReactNode;
  label: string;
}) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
      {icon} {label}
    </span>
  );
}

/* -------------------------------------------------------------------- */
/* Variant B — "Two-lane split"                                         */
/* The card is bisected: researcher lane on the left ("what can I       */
/* run?"), operations lane on the right ("is it healthy?"). Each lane   */
/* has its own subtitle so neither persona has to learn the other's     */
/* terminology.                                                         */
/* -------------------------------------------------------------------- */

function VariantB() {
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
      {/* Single thin name strip on top */}
      <div
        style={{
          padding: "10px 16px",
          display: "flex",
          alignItems: "center",
          gap: 10,
          borderBottom: "1px solid var(--border-weak)",
          fontSize: 13,
        }}
      >
        <ChevronDown size={12} color="var(--text-faint)" />
        <strong>{MOCK.name}</strong>
        <span style={{ color: "var(--text-faint)", fontSize: 11 }}>
          · {MOCK.region}
        </span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <button style={btnStyle("warning")}>
            <Square size={11} /> Stop
          </button>
          <button style={btnStyle("danger")}>
            <Trash2 size={11} />
          </button>
        </div>
      </div>

      {/* Two columns — researcher | operations */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1.2fr 1fr",
          gap: 0,
        }}
      >
        {/* Researcher lane */}
        <div
          style={{
            padding: "16px 18px",
            borderRight: "1px solid var(--border-weak)",
          }}
        >
          <LaneHeading
            color="var(--accent)"
            title="What you can run"
            subtitle="Databases ready to query"
          />
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
            <StatusDot color="var(--success)" size={10} />
            <span style={{ fontSize: 13, color: "var(--text-primary)" }}>
              Execution environment is ready
            </span>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {MOCK.readyDbs.map((db) => (
              <DbRow
                key={db.name}
                name={db.name}
                detail={`${db.sizeGb} GB · warmed`}
                tone="success"
              />
            ))}
            {MOCK.warmingDbs.map((name) => (
              <DbRow
                key={name}
                name={name}
                detail="loading into node cache…"
                tone="warning"
              />
            ))}
            {MOCK.unavailableDbs.map((name) => (
              <DbRow
                key={name}
                name={name}
                detail="not loaded — click to prepare"
                tone="muted"
              />
            ))}
          </div>
        </div>

        {/* Operations lane */}
        <div
          style={{
            padding: "16px 18px",
            background: "rgba(255,255,255,0.015)",
          }}
        >
          <LaneHeading
            color="var(--teal)"
            title="Cluster health"
            subtitle="Live operational state"
          />
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "1fr 1fr",
              gap: 10,
              marginBottom: 12,
            }}
          >
            <OpsTile
              label="Power"
              value={MOCK.powerState}
              tone={MOCK.powerState === "Running" ? "success" : "warning"}
            />
            <OpsTile
              label="Provisioning"
              value={MOCK.provisioningState}
              tone={MOCK.provisioningState === "Succeeded" ? "success" : "warning"}
            />
            <OpsTile label="Nodes" value={`${MOCK.totalNodes}`} tone="neutral" />
            <OpsTile label="k8s" value={MOCK.k8sVersion} tone="neutral" />
          </div>
          <div
            style={{
              fontSize: 10,
              fontWeight: 600,
              color: "var(--text-faint)",
              textTransform: "uppercase",
              letterSpacing: "0.08em",
              marginBottom: 6,
            }}
          >
            Node pools
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {MOCK.pools.map((p) => (
              <div
                key={p.name}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  fontSize: 11,
                  color: "var(--text-muted)",
                }}
              >
                <span>
                  {p.role === "system" ? "system" : "user"} · {p.name}
                </span>
                <span style={{ fontFamily: "var(--font-mono)" }}>
                  {p.nodes}× {p.sku}
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function LaneHeading({
  color,
  title,
  subtitle,
}: {
  color: string;
  title: string;
  subtitle: string;
}) {
  return (
    <div style={{ marginBottom: 10 }}>
      <div
        style={{
          fontSize: 10,
          fontWeight: 700,
          letterSpacing: "0.1em",
          color,
          textTransform: "uppercase",
        }}
      >
        {title}
      </div>
      <div style={{ fontSize: 11, color: "var(--text-faint)" }}>{subtitle}</div>
    </div>
  );
}

function DbRow({
  name,
  detail,
  tone,
}: {
  name: string;
  detail: string;
  tone: "success" | "warning" | "muted";
}) {
  const color =
    tone === "success"
      ? "var(--success)"
      : tone === "warning"
        ? "var(--warning)"
        : "var(--text-faint)";
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "6px 8px",
        borderRadius: 6,
        background: tone === "muted" ? "transparent" : `${color}10`,
        border: tone === "muted" ? "1px dashed var(--border-weak)" : `1px solid ${color}30`,
      }}
    >
      <StatusDot color={color} size={8} />
      <span style={{ fontSize: 12, color: "var(--text-primary)", fontWeight: 500 }}>
        {name}
      </span>
      <span style={{ fontSize: 11, color: "var(--text-muted)", marginLeft: "auto" }}>
        {detail}
      </span>
    </div>
  );
}

function OpsTile({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: "success" | "warning" | "neutral";
}) {
  const color =
    tone === "success"
      ? "var(--success)"
      : tone === "warning"
        ? "var(--warning)"
        : "var(--text-primary)";
  return (
    <div
      style={{
        padding: "8px 10px",
        background: "var(--bg-secondary)",
        borderRadius: 6,
        border: "1px solid var(--border-weak)",
      }}
    >
      <div
        style={{
          fontSize: 10,
          color: "var(--text-faint)",
          textTransform: "uppercase",
          letterSpacing: "0.06em",
        }}
      >
        {label}
      </div>
      <div style={{ fontSize: 14, fontWeight: 600, color, marginTop: 2 }}>
        {value}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Variant C — "KPI strip + progressive disclosure"                     */
/* Four large KPI tiles deliver the 3-second scan; everything else      */
/* lives inside collapsed accordion rows ("Databases", "Node pools"),   */
/* expanded by default for researchers but collapsable for ops.         */
/* -------------------------------------------------------------------- */

function VariantC() {
  const [openDb, setOpenDb] = useState(true);
  const [openPools, setOpenPools] = useState(false);
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
        <strong style={{ fontSize: 14 }}>{MOCK.name}</strong>
        <span style={{ fontSize: 11, color: "var(--text-faint)" }}>
          ElasticBLAST execution environment
        </span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <button style={btnStyle("warning")}>
            <Square size={11} /> Stop
          </button>
          <button style={btnStyle("danger")}>
            <Trash2 size={11} />
          </button>
        </div>
      </div>

      {/* 4 KPI tiles */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(4, 1fr)",
          gap: 1,
          background: "var(--border-weak)",
        }}
      >
        <KpiTile
          icon={<CheckCircle2 size={16} color="var(--success)" />}
          label="Status"
          value="Healthy"
          accent="var(--success)"
        />
        <KpiTile
          icon={<Database size={16} color="var(--accent)" />}
          label="Ready DBs"
          value={`${MOCK.readyDbs.length} / ${MOCK.readyDbs.length + MOCK.warmingDbs.length + MOCK.unavailableDbs.length}`}
          accent="var(--accent)"
        />
        <KpiTile
          icon={<Zap size={16} color="var(--warning)" />}
          label="Active jobs"
          value={`${MOCK.activeJobs}`}
          accent="var(--warning)"
        />
        <KpiTile
          icon={<HardDrive size={16} color="var(--teal)" />}
          label="Nodes"
          value={`${MOCK.totalNodes}`}
          accent="var(--teal)"
          sub={`k8s ${MOCK.k8sVersion}`}
        />
      </div>

      {/* Accordion: databases */}
      <Accordion
        open={openDb}
        onToggle={() => setOpenDb((v) => !v)}
        title="Databases"
        badge={`${MOCK.readyDbs.length} ready · ${MOCK.warmingDbs.length} warming`}
      >
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {MOCK.readyDbs.map((db) => (
            <Chip key={db.name} tone="success">
              <Flame size={11} /> {db.name}
            </Chip>
          ))}
          {MOCK.warmingDbs.map((name) => (
            <Chip key={name} tone="warning">
              warming · {name}
            </Chip>
          ))}
          {MOCK.unavailableDbs.map((name) => (
            <Chip key={name} size="sm">
              {name}
            </Chip>
          ))}
        </div>
      </Accordion>

      {/* Accordion: pools */}
      <Accordion
        open={openPools}
        onToggle={() => setOpenPools((v) => !v)}
        title="Node pools"
        badge={`${MOCK.pools.length} pools · ${MOCK.totalNodes} nodes`}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {MOCK.pools.map((p) => (
            <div
              key={p.name}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "6px 10px",
                background: "var(--bg-secondary)",
                borderRadius: 6,
                fontSize: 12,
              }}
            >
              <span>
                <strong>{p.name}</strong>{" "}
                <span style={{ color: "var(--text-faint)" }}>· {p.role}</span>
              </span>
              <span style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                {p.nodes}× {p.sku}
              </span>
            </div>
          ))}
        </div>
      </Accordion>
    </div>
  );
}

function KpiTile({
  icon,
  label,
  value,
  accent,
  sub,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  accent: string;
  sub?: string;
}) {
  return (
    <div
      style={{
        padding: "14px 16px",
        background: "var(--bg-primary)",
        display: "flex",
        flexDirection: "column",
        gap: 4,
        position: "relative",
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
          opacity: 0.5,
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
      </div>
      <div
        style={{
          fontSize: 22,
          fontWeight: 700,
          color: "var(--text-primary)",
          letterSpacing: "-0.02em",
        }}
      >
        {value}
      </div>
      {sub && (
        <div style={{ fontSize: 10, color: "var(--text-faint)" }}>{sub}</div>
      )}
    </div>
  );
}

function Accordion({
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
          padding: "10px 18px",
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
      {open && <div style={{ padding: "4px 18px 14px" }}>{children}</div>}
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Shared button style                                                   */
/* -------------------------------------------------------------------- */
function btnStyle(
  tone: "warning" | "danger" | "success",
): React.CSSProperties {
  const color =
    tone === "warning"
      ? "var(--warning)"
      : tone === "danger"
        ? "var(--danger)"
        : "var(--success)";
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

/* -------------------------------------------------------------------- */
/* Page                                                                  */
/* -------------------------------------------------------------------- */

export function AksCardMockups() {
  return (
    <div style={{ padding: "32px 24px", maxWidth: 1080, margin: "0 auto" }}>
      <style>
        {`@keyframes elbPulse {
          0%,100% { box-shadow: 0 0 0 0 currentColor; }
          50%    { box-shadow: 0 0 0 6px transparent; }
        }`}
      </style>
      <div style={{ marginBottom: 32 }}>
        <h1 style={{ fontSize: 22, marginBottom: 6, color: "var(--text-primary)" }}>
          AKS card redesign — proposals
        </h1>
        <p style={{ fontSize: 13, color: "var(--text-muted)", marginTop: 0 }}>
          Three layouts on identical data. Pick the one that best balances{" "}
          <em>{PERSONA_COPY.researcher}</em> and <em>{PERSONA_COPY.it}</em>{" "}
          comprehension. The current card is in{" "}
          <code>web/src/components/ClusterItem/ClusterItem.tsx</code>.
        </p>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 36 }}>
        <section>
          <SectionHeading
            persona={`${PERSONA_COPY.researcher} (primary)`}
            variant="Variant A"
            title="Traffic-light hero"
            subtitle="One dominant verdict, a single DB strip, IT details in a small footer. Reads top-to-bottom; the researcher only needs the first two lines."
          />
          <VariantA />
        </section>

        <section>
          <SectionHeading
            persona="both, side-by-side"
            variant="Variant B"
            title="Two-lane split"
            subtitle="Card is bisected: left lane answers “what can I run?”, right lane answers “is it healthy?”. Each lane uses vocabulary appropriate to its audience."
          />
          <VariantB />
        </section>

        <section>
          <SectionHeading
            persona={`${PERSONA_COPY.it} (primary)`}
            variant="Variant C"
            title="KPI strip + progressive disclosure"
            subtitle="Four large KPI tiles deliver the 3-second scan; databases and pools live in accordion rows. Databases open by default, pools collapsed."
          />
          <VariantC />
        </section>
      </div>

      <div
        style={{
          marginTop: 48,
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
            <strong>Variant A</strong> is the safest upgrade — it preserves
            roughly the current information density but rewrites the hero
            band so a non-AKS user sees “BLAST execution environment is
            ready”, not “Running · k8s 1.34.0 · 4 nodes”.
          </li>
          <li>
            <strong>Variant B</strong> is the most explicit about the two
            personas but uses the most horizontal space; it shines on the
            Dashboard but might feel heavy if dropped inside narrower
            panels.
          </li>
          <li>
            <strong>Variant C</strong> wins the 3-second scan for IT and
            keeps everything else one click away. Best if we expect more
            than two clusters per workspace, since the KPI strip stays
            uniform while collapsed accordions keep vertical height bounded.
          </li>
        </ul>
      </div>
    </div>
  );
}

export default AksCardMockups;
