import { AlertTriangle, ArrowRight, Database } from "lucide-react";
import { Link } from "react-router-dom";

import { NotImplementedBanner } from "@/pages/tools/ToolLayout";

import { BuildSection } from "./BuildSection";
import { DbConfigSection } from "./DbConfigSection";
import { ExistingDbsSection } from "./ExistingDbsSection";
import { FastaInputSection } from "./FastaInputSection";
import { useDatabaseBuilderState } from "./useDatabaseBuilderState";

export function DatabaseBuilder() {
  const state = useDatabaseBuilderState();
  const { cfg, readiness, readyCount } = state;

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
