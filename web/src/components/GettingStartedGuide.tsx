import { useState } from "react";
import { Link } from "react-router-dom";
import {
  Rocket, Server, Package, Terminal, Database,
  CheckCircle2, ArrowRight, X, ChevronRight, Sparkles,
} from "lucide-react";

interface Props {
  hasCluster: boolean;
  hasImages: boolean;
  hasTerminal: boolean;
  clusterRunning: boolean;
  acrName: string;
  onDismiss: () => void;
}

interface Step {
  id: string;
  title: string;
  description: string;
  icon: React.ReactNode;
  done: boolean;
  action: { label: string; to: string } | null;
  detail?: string;
}

export function GettingStartedGuide({
  hasCluster, hasImages, hasTerminal, clusterRunning, acrName, onDismiss,
}: Props) {
  const [expanded, setExpanded] = useState<string | null>(null);

  const steps: Step[] = [
    {
      id: "images",
      title: "Build container images",
      description: "Build ElasticBLAST container images in your Azure Container Registry. This takes about 5 minutes.",
      icon: <Package size={18} />,
      done: hasImages,
      action: hasImages ? null : { label: "Go to Dashboard → ACR card → Build All", to: "/" },
      detail: hasImages
        ? `All 4 images built in ${acrName}`
        : `Open the ACR card on the Dashboard and click "Build All Images". Required images: ncbi/elb, ncbi/elasticblast-job-submit, ncbi/elasticblast-query-split, elb-openapi.`,
    },
    {
      id: "cluster",
      title: "Create an AKS cluster",
      description: "Provision an Azure Kubernetes Service cluster to run BLAST searches. Takes 5–10 minutes.",
      icon: <Server size={18} />,
      done: hasCluster,
      action: hasCluster ? null : { label: "Go to Dashboard → AKS card → Add Cluster", to: "/" },
      detail: hasCluster
        ? clusterRunning ? "Cluster is running" : "Cluster exists but is stopped. Start it from the Dashboard."
        : 'Click "+ Add Cluster" on the AKS card. Recommended: Standard_E16s_v5, 3 nodes for standard workloads. For large databases (nt, nr), use Standard_E32s_v5 or higher.',
    },
    {
      id: "terminal",
      title: "Open the Terminal",
      description: "The browser terminal sidecar (with elastic-blast CLI pre-installed) ships with the deployment and is reached over an authenticated WebSocket.",
      icon: <Terminal size={18} />,
      done: hasTerminal,
      action: hasTerminal ? null : { label: "Open Terminal page", to: "/terminal" },
      detail: hasTerminal
        ? "Terminal sidecar is healthy"
        : "The `terminal` sidecar is not available in this environment. It ships with the deployed Container App (or a local `docker compose -f scripts/dev/docker-compose.local.yml up` stack). Running the api alone is enough for the rest of the dashboard.",
    },
    {
      id: "database",
      title: "Download a BLAST database",
      description: "Download a reference database from NCBI to your storage account. Size varies by database.",
      icon: <Database size={18} />,
      done: false, // Always shown as a recommendation
      action: { label: "Go to Dashboard → Storage card → Download DB", to: "/" },
      detail: 'Recommended databases:\n• 16S_ribosomal_RNA (~18 MB) — fastest, good for testing\n• core_nt (~250 GB) — core nucleotide, most common\n• nt (~400 GB) — full nucleotide collection\n• nr (~300 GB) — non-redundant protein\n\nOpen the Storage card, click "Download from NCBI" and select a database.',
    },
  ];

  const completedCount = steps.filter(s => s.done).length;
  const totalSteps = steps.length;
  const progress = (completedCount / totalSteps) * 100;

  return (
    <div
      style={{
        position: "fixed", inset: 0,
        background: "rgba(0,0,0,0.6)", backdropFilter: "blur(4px)",
        display: "flex", alignItems: "center", justifyContent: "center",
        zIndex: 150,
      }}
      onClick={(e) => { if (e.target === e.currentTarget) onDismiss(); }}
    >
      <div style={{
        background: "var(--bg-primary)", border: "1px solid var(--border-medium)",
        borderRadius: 16, boxShadow: "0 12px 48px rgba(0,0,0,0.5)",
        width: 560, maxHeight: "85vh", overflow: "hidden",
        display: "flex", flexDirection: "column",
      }}>
        {/* Header */}
        <div style={{
          padding: "24px 28px 16px",
          background: "linear-gradient(135deg, rgba(110,159,255,0.08) 0%, rgba(184,119,217,0.06) 100%)",
          borderBottom: "1px solid var(--border-weak)",
        }}>
          <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <div style={{
                width: 44, height: 44, borderRadius: 12,
                background: "linear-gradient(135deg, var(--accent), var(--purple))",
                display: "grid", placeItems: "center",
                boxShadow: "0 4px 16px rgba(110,159,255,0.3)",
              }}>
                <Rocket size={22} style={{ color: "#fff" }} />
              </div>
              <div>
                <h2 style={{ margin: 0, fontSize: 18, fontWeight: 700, letterSpacing: "-0.02em" }}>
                  Getting Started
                </h2>
                <p style={{ margin: "2px 0 0", fontSize: 12, color: "var(--text-muted)" }}>
                  Complete these steps to start running BLAST searches
                </p>
              </div>
            </div>
            <button
              onClick={onDismiss}
              style={{
                width: 32, height: 32, borderRadius: 8, display: "grid", placeItems: "center",
                background: "none", border: "none", color: "var(--text-faint)", cursor: "pointer",
              }}
              title="Dismiss (won't show again this session)"
            >
              <X size={18} />
            </button>
          </div>

          {/* Progress bar */}
          <div style={{ marginTop: 16, display: "flex", alignItems: "center", gap: 12 }}>
            <div style={{
              flex: 1, height: 6, borderRadius: 3,
              background: "var(--bg-tertiary)", overflow: "hidden",
            }}>
              <div style={{
                width: `${progress}%`, height: "100%", borderRadius: 3,
                background: "linear-gradient(90deg, var(--accent), var(--purple))",
                transition: "width 0.5s ease-out",
              }} />
            </div>
            <span style={{ fontSize: 11, color: "var(--text-faint)", fontFamily: "var(--font-mono)" }}>
              {completedCount}/{totalSteps}
            </span>
          </div>
        </div>

        {/* Steps */}
        <div style={{ padding: "12px 28px 24px", overflowY: "auto", flex: 1 }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {steps.map((step) => (
              <StepItem
                key={step.id}
                step={step}
                expanded={expanded === step.id}
                onToggle={() => setExpanded(expanded === step.id ? null : step.id)}
                onDismiss={onDismiss}
              />
            ))}
          </div>
        </div>

        {/* Footer */}
        <div style={{
          padding: "12px 28px", borderTop: "1px solid var(--border-weak)",
          display: "flex", justifyContent: "space-between", alignItems: "center",
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11, color: "var(--text-faint)" }}>
            <Sparkles size={12} />
            Press <kbd style={{
              padding: "1px 5px", fontSize: 10, fontFamily: "var(--font-mono)",
              background: "var(--bg-tertiary)", border: "1px solid var(--border-weak)",
              borderRadius: 3,
            }}>?</kbd> anytime for help
          </div>
          <button
            onClick={onDismiss}
            style={{
              background: "none", border: "none", color: "var(--accent)",
              cursor: "pointer", fontSize: 12, padding: "4px 0",
            }}
          >
            I'll set up later
          </button>
        </div>
      </div>
    </div>
  );
}

function StepItem({ step, expanded, onToggle, onDismiss }: {
  step: Step; expanded: boolean; onToggle: () => void; onDismiss: () => void;
}) {
  return (
    <div style={{
      borderRadius: 10, overflow: "hidden",
      border: `1px solid ${expanded ? "var(--border-medium)" : "var(--border-weak)"}`,
      transition: "border-color 0.15s",
    }}>
      <button
        onClick={onToggle}
        style={{
          display: "flex", alignItems: "center", gap: 12, width: "100%",
          padding: "14px 16px", background: "none", border: "none",
          cursor: "pointer", color: "inherit", textAlign: "left",
        }}
      >
        {/* Status icon */}
        <div style={{
          width: 32, height: 32, borderRadius: 8, flexShrink: 0,
          display: "grid", placeItems: "center",
          background: step.done ? "rgba(115,191,105,0.1)" : "var(--bg-tertiary)",
          color: step.done ? "var(--success)" : "var(--text-faint)",
        }}>
          {step.done ? <CheckCircle2 size={18} /> : step.icon}
        </div>

        <div style={{ flex: 1 }}>
          <div style={{
            fontSize: 13, fontWeight: 600,
            color: step.done ? "var(--text-muted)" : "var(--text-primary)",
            textDecoration: step.done ? "line-through" : "none",
            opacity: step.done ? 0.7 : 1,
          }}>
            {step.title}
          </div>
          <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 1 }}>
            {step.description}
          </div>
        </div>

        <ChevronRight size={14} style={{
          color: "var(--text-faint)", flexShrink: 0,
          transform: expanded ? "rotate(90deg)" : "none",
          transition: "transform 0.15s",
        }} />
      </button>

      {expanded && (
        <div style={{
          padding: "0 16px 14px", borderTop: "1px solid var(--border-weak)",
        }}>
          {step.detail && (
            <div style={{
              fontSize: 12, color: "var(--text-muted)", lineHeight: 1.7,
              marginTop: 12, whiteSpace: "pre-line",
            }}>
              {step.detail}
            </div>
          )}
          {step.action && !step.done && (
            <Link
              to={step.action.to}
              onClick={onDismiss}
              style={{
                display: "inline-flex", alignItems: "center", gap: 6,
                marginTop: 12, padding: "7px 14px", borderRadius: 8,
                background: "rgba(110,159,255,0.1)", border: "1px solid rgba(110,159,255,0.25)",
                color: "var(--accent)", fontSize: 12, fontWeight: 500,
                textDecoration: "none", transition: "all 0.15s",
              }}
            >
              {step.action.label} <ArrowRight size={12} />
            </Link>
          )}
        </div>
      )}
    </div>
  );
}
