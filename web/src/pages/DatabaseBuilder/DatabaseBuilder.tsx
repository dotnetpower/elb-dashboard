import { AlertTriangle, ArrowRight, Check, Database } from "lucide-react";
import { Link } from "react-router-dom";

import { NotImplementedBanner } from "@/pages/tools/ToolLayout";

import { BuildSection } from "./BuildSection";
import { DbConfigSection } from "./DbConfigSection";
import { ExistingDbsSection } from "./ExistingDbsSection";
import { FastaInputSection } from "./FastaInputSection";
import { useDatabaseBuilderState } from "./useDatabaseBuilderState";

// C1: 3-step wizard model. "done" = inputs satisfied, "active" = next missing
// step, "pending" = downstream. Renders as a compact horizontal stepper just
// below the page header so the user always knows where they are.
type StepState = "done" | "active" | "pending";

interface WizardStep {
  key: string;
  label: string;
  desc: string;
  state: StepState;
}

function WizardStepper({ steps }: { steps: WizardStep[] }) {
  return (
    <ol
      className="db-wizard-stepper"
      aria-label="Custom DB build progress"
      style={{
        display: "flex",
        gap: 8,
        listStyle: "none",
        padding: 0,
        margin: 0,
        flexWrap: "wrap",
      }}
    >
      {steps.map((step, idx) => {
        const color =
          step.state === "done"
            ? "var(--success)"
            : step.state === "active"
              ? "var(--accent)"
              : "var(--text-faint)";
        const background =
          step.state === "done"
            ? "rgba(106,214,163,0.10)"
            : step.state === "active"
              ? "rgba(122,167,255,0.10)"
              : "var(--bg-secondary)";
        return (
          <li
            key={step.key}
            aria-current={step.state === "active" ? "step" : undefined}
            style={{
              flex: "1 1 200px",
              minWidth: 180,
              padding: "8px 12px",
              borderRadius: 10,
              border: `1px solid ${
                step.state === "pending" ? "var(--border-weak)" : color
              }`,
              background,
              display: "flex",
              alignItems: "center",
              gap: 10,
            }}
          >
            <span
              aria-hidden
              style={{
                width: 22,
                height: 22,
                borderRadius: 999,
                background: color,
                color: "#0b132b",
                fontSize: 11,
                fontWeight: 700,
                display: "grid",
                placeItems: "center",
                flexShrink: 0,
              }}
            >
              {step.state === "done" ? <Check size={12} strokeWidth={2.5} /> : idx + 1}
            </span>
            <div style={{ display: "flex", flexDirection: "column" }}>
              <span style={{ fontSize: 12, fontWeight: 600, color: "var(--text-primary)" }}>
                {step.label}
              </span>
              <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
                {step.desc}
              </span>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

export function DatabaseBuilder() {
  const state = useDatabaseBuilderState();
  const { cfg, readiness, readyCount } = state;

  // C1: derive stepper state from the existing builder state so we don't
  // duplicate validation rules. Build is "done" only once the mutation has
  // actually produced output (state.successPath).
  const configDone =
    state.isValidDbName && !state.nameClash && !!state.dbType;
  const fastaDone = state.fastaStats.isValid;
  const buildDone = !!state.successPath;
  const buildActive = !buildDone && configDone && fastaDone;
  const fastaActive = !fastaDone && configDone;
  const configActive = !configDone;
  const stepperSteps: WizardStep[] = [
    {
      key: "configure",
      label: "Configure",
      desc: "Name + molecule type",
      state: configDone ? "done" : configActive ? "active" : "pending",
    },
    {
      key: "input",
      label: "Provide FASTA",
      desc: state.fastaStats.seqCount
        ? `${state.fastaStats.seqCount} sequences staged`
        : "Paste or upload sequences",
      state: fastaDone ? "done" : fastaActive ? "active" : "pending",
    },
    {
      key: "build",
      label: "Build & publish",
      desc: buildDone
        ? "Published to Blob Storage"
        : state.buildMutation.isPending
          ? "makeblastdb running…"
          : "Run makeblastdb in the sidecar",
      state: buildDone ? "done" : buildActive ? "active" : "pending",
    },
  ];

  return (
    <div className="page-stack mono-page custom-db-page">
      <header
        className="page-header mono-header"
        style={{
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "space-between",
          gap: 16,
          flexWrap: "wrap",
          marginBottom: 0,
        }}
      >
        <div>
          <div
            className="page-header__title"
            style={{ display: "flex", alignItems: "center", gap: 10 }}
          >
            <Database size={22} strokeWidth={1.5} style={{ color: "var(--accent)" }} />
            ElasticBLAST Custom DB
          </div>
          <div className="page-header__desc">
            Upload FASTA sequences, run <code className="code-val">makeblastdb</code> in
            the terminal sidecar, and publish a private BLAST database to Azure Blob
            Storage.
          </div>
        </div>
        <div
          className="blast-readiness"
          aria-label="Builder readiness"
          title={`${readyCount} of ${readiness.length} prerequisites ready`}
        >
          {readiness.map((r) => (
            <span
              key={r.label}
              className={`blast-readiness__dot${r.ok ? " blast-readiness__dot--ok" : ""}`}
              title={r.label}
            />
          ))}
          <span className="muted" style={{ fontSize: 10 }}>
            {readyCount}/{readiness.length}
          </span>
        </div>
      </header>

      <WizardStepper steps={stepperSteps} />

      <NotImplementedBanner feature="Custom Database Builder" />

      {!cfg?.subscriptionId && (
        <section
          className="glass-card"
          style={{
            padding: 16,
            display: "flex",
            alignItems: "center",
            gap: 12,
            borderColor: "var(--warning)",
          }}
        >
          <AlertTriangle size={18} style={{ color: "var(--warning)", flexShrink: 0 }} />
          <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 600, fontSize: 13 }}>Workspace not configured</div>
            <div className="muted" style={{ fontSize: 12 }}>
              Pick a subscription and storage account on the Dashboard before building
              a custom database.
            </div>
          </div>
          <Link to="/" className="btn btn--primary btn--sm">
            Open Dashboard <ArrowRight size={12} />
          </Link>
        </section>
      )}

      <DbConfigSection
        dbName={state.dbName}
        setDbName={state.setDbName}
        dbType={state.dbType}
        setDbType={state.setDbType}
        title={state.title}
        setTitle={state.setTitle}
        isValidDbName={state.isValidDbName}
        nameClash={state.nameClash}
      />

      <FastaInputSection
        fastaData={state.fastaData}
        setFastaData={state.setFastaData}
        inputMode={state.inputMode}
        setInputMode={state.setInputMode}
        fileName={state.fileName}
        setFileName={state.setFileName}
        fastaStats={state.fastaStats}
        handleFileUpload={state.handleFileUpload}
        setDbName={state.setDbName}
        setDbType={state.setDbType}
        setTitle={state.setTitle}
      />

      <BuildSection
        dbName={state.dbName}
        dbType={state.dbType}
        fastaStats={state.fastaStats}
        readiness={state.readiness}
        readyCount={state.readyCount}
        allReady={state.allReady}
        buildMutation={state.buildMutation}
        successPath={state.successPath}
        copied={state.copied}
        handleCopyPath={state.handleCopyPath}
      />

      <ExistingDbsSection
        cfg={state.cfg}
        dbListQuery={state.dbListQuery}
        existingDbs={state.existingDbs}
      />
    </div>
  );
}
