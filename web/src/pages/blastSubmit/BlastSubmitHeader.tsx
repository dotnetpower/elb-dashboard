import { Dna, RotateCcw } from "lucide-react";

import type { ProgramMeta } from "./types";

export interface BlastSubmitHeaderProps {
  programMeta: ProgramMeta;
  readySteps: { ok: boolean; label: string }[];
  readyCount: number;
  onReset: () => void;
}

export function BlastSubmitHeader({
  programMeta,
  readySteps,
  readyCount,
  onReset,
}: BlastSubmitHeaderProps) {
  const flavor =
    programMeta.label === "blastn"
      ? "Standard Nucleotide"
      : programMeta.label === "blastp"
        ? "Standard Protein"
        : programMeta.label.toUpperCase();
  return (
    <header className="blast-header">
      <div>
        <div className="blast-header__title">
          <Dna size={24} strokeWidth={1.5} style={{ color: "var(--accent)" }} />
          <h1 style={{ margin: 0 }}>
            ElasticBLAST New Search · {flavor} BLAST
          </h1>
        </div>
        <p className="muted" style={{ marginTop: 4, fontSize: 13 }}>
          Submit a sequence search using ElasticBLAST on AKS
        </p>
      </div>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <div className="blast-readiness">
          {readySteps.map((s) => (
            <span
              key={s.label}
              className={`blast-readiness__dot${s.ok ? " blast-readiness__dot--ok" : ""}`}
              title={s.label}
            />
          ))}
          <span className="muted" style={{ fontSize: 10 }}>
            {readyCount}/{readySteps.length}
          </span>
        </div>
        <button
          className="glass-button"
          onClick={onReset}
          style={{ fontSize: 11 }}
        >
          <RotateCcw size={12} strokeWidth={1.5} /> Reset
        </button>
      </div>
    </header>
  );
}
