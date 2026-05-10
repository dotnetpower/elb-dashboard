import { useState } from "react";
import { Link } from "react-router-dom";
import { CheckCircle2, Circle, ArrowRight, X } from "lucide-react";
import type { ResourceConfig } from "@/components/SetupWizard";

interface Props {
  config: ResourceConfig;
  hasCluster?: boolean;
  hasStorage?: boolean;
  hasAcr?: boolean;
  jobCount?: number;
}

interface Step {
  id: string;
  label: string;
  description: string;
  done: boolean;
  link?: string;
  linkLabel?: string;
}

export function GettingStarted({ config, hasCluster, hasStorage, hasAcr, jobCount }: Props) {
  const [dismissed, setDismissed] = useState(() => {
    try { return localStorage.getItem("elb-getting-started-dismissed") === "1"; } catch { return false; }
  });

  const steps: Step[] = [
    {
      id: "config",
      label: "Configure workspace",
      description: "Connect to your Azure subscription and select resource groups.",
      done: Boolean(config.subscriptionId && config.workloadResourceGroup),
    },
    {
      id: "cluster",
      label: "Create AKS cluster",
      description: "Use the AKS Cluster card below to provision a Kubernetes cluster.",
      done: Boolean(hasCluster),
    },
    {
      id: "images",
      label: "Build container images",
      description: "Use the ACR card below to build the ElasticBLAST Docker images.",
      done: Boolean(hasAcr),
    },
    {
      id: "db",
      label: "Download a BLAST database",
      description: "Use the Storage card below to copy a database from NCBI.",
      done: Boolean(hasStorage),
      link: "/",
      linkLabel: "Dashboard",
    },
    {
      id: "search",
      label: "Run your first BLAST search",
      description: "Submit a FASTA query and let ElasticBLAST find matches.",
      done: (jobCount ?? 0) > 0,
      link: "/blast/submit",
      linkLabel: "New search",
    },
  ];

  const completedCount = steps.filter((s) => s.done).length;
  const allDone = completedCount === steps.length;
  const nextStep = steps.find((s) => !s.done);

  // Auto-hide when all steps are done
  if (dismissed || allDone) return null;

  return (
    <div className="panel" style={{ marginBottom: "var(--space-3)", borderLeft: "2px solid var(--accent)" }}>
      <div className="panel-hd" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div>
          <div className="title">Getting Started</div>
          <div className="sub">{completedCount}/{steps.length} steps completed</div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div className="prog" style={{ width: 100 }}>
            <div className="prog-f" style={{ width: `${(completedCount / steps.length) * 100}%`, background: "var(--accent)" }} />
          </div>
          <button
            className="glass-button"
            onClick={() => {
              setDismissed(true);
              try { localStorage.setItem("elb-getting-started-dismissed", "1"); } catch { /* */ }
            }}
            style={{ padding: "2px 6px", border: "none", background: "none" }}
            title="Dismiss"
          >
            <X size={14} />
          </button>
        </div>
      </div>
      <div className="panel-bd" style={{ padding: "10px 14px", minHeight: "auto" }}>
        {/* Highlight next action */}
        {nextStep && (
          <div style={{
            display: "flex", alignItems: "center", justifyContent: "space-between",
            padding: "10px 12px", marginBottom: 8, borderRadius: 6,
            background: "rgba(110,159,255,0.06)", border: "1px solid rgba(110,159,255,0.15)",
          }}>
            <div>
              <div style={{ fontSize: 13, fontWeight: 600 }}>Next: {nextStep.label}</div>
              <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>{nextStep.description}</div>
            </div>
            {nextStep.link && (
              <Link to={nextStep.link} className="glass-button glass-button--primary" style={{ fontSize: 11, whiteSpace: "nowrap" }}>
                {nextStep.linkLabel} <ArrowRight size={12} />
              </Link>
            )}
          </div>
        )}
        {/* Step list */}
        <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 6 }}>
          {steps.map((s) => (
            <div key={s.id} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
              {s.done ? (
                <CheckCircle2 size={14} style={{ color: "var(--success)", flexShrink: 0 }} />
              ) : (
                <Circle size={14} style={{ color: "var(--text-faint)", flexShrink: 0 }} />
              )}
              <span style={{ color: s.done ? "var(--text-muted)" : "var(--text-primary)", textDecoration: s.done ? "line-through" : "none" }}>
                {s.label}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
