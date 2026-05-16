import { describe, expect, it } from "vitest";

import {
  analyzeDiagnosticReadiness,
  analyzeDiagnosticTriage,
  buildDiagnosticRunbookDraft,
  DEFAULT_DIAGNOSTIC_CONTEXT,
  DIAGNOSTIC_WORKFLOWS,
  parseBlastOutfmt6,
  triageBlastOutfmt6,
} from "./terminalDiagnosticModel";

describe("terminal diagnostic model", () => {
  it("defines molecular-diagnostic workflow presets", () => {
    expect(DIAGNOSTIC_WORKFLOWS.map((workflow) => workflow.id)).toEqual([
      "pathogen-id",
      "sixteen-s-its",
      "amr-screen",
      "primer-specificity",
      "custom-db-validation",
    ]);
    expect(DIAGNOSTIC_WORKFLOWS.every((workflow) => workflow.recommendedCommands.length > 0)).toBe(
      true,
    );
  });

  it("warns when primer specificity misses blastn-short", () => {
    const guards = analyzeDiagnosticReadiness("blastn -query primers.fa -db nt -outfmt 6", {
      ...DEFAULT_DIAGNOSTIC_CONTEXT,
      workflowId: "primer-specificity",
      inputType: "primers",
      database: "nt release 2026-05",
      sampleId: "PRIMER-01",
    });

    expect(guards.map((guard) => guard.message)).toContain(
      "Primer/probe checks should usually use -task blastn-short.",
    );
  });

  it("flags makeblastdb without explicit dbtype", () => {
    const guards = analyzeDiagnosticReadiness("makeblastdb -in refs.fa -out refs", {
      ...DEFAULT_DIAGNOSTIC_CONTEXT,
      workflowId: "custom-db-validation",
      database: "custom-db v1",
      sampleId: "DB-01",
    });

    expect(guards).toContainEqual({
      level: "critical",
      message: "makeblastdb needs an explicit -dbtype nucl or -dbtype prot.",
    });
  });

  it("warns on sample ids that look identifying", () => {
    const guards = analyzeDiagnosticReadiness("seqkit stats *.fa", {
      ...DEFAULT_DIAGNOSTIC_CONTEXT,
      sampleId: "patient-12345678",
    });

    expect(guards).toContainEqual({
      level: "critical",
      message: "Sample id may contain identifying data; use a de-identified lab accession.",
    });
  });

  it("parses BLAST outfmt 6 with optional qcovs", () => {
    const hits = parseBlastOutfmt6(
      "q1\tsubjectA\t99.1\t1400\t1\t0\t1\t1400\t5\t1404\t1e-120\t500\t96",
    );

    expect(hits[0]).toMatchObject({
      queryId: "q1",
      subjectId: "subjectA",
      identity: 99.1,
      queryCoverage: 96,
      bitScore: 500,
    });
  });

  it("triages near-tie hits as requiring review", () => {
    const triage = triageBlastOutfmt6(
      [
        "q1\tspeciesA\t99.5\t1450\t0\t0\t1\t1450\t1\t1450\t1e-100\t500\t98",
        "q1\tspeciesB\t99.4\t1450\t0\t0\t1\t1450\t1\t1450\t1e-99\t496\t98",
      ].join("\n"),
      "sixteen-s-its",
    );

    expect(triage.evidenceLevel).toBe("review");
    expect(triage.ambiguousTopHits).toHaveLength(1);
    expect(triage.warnings).toContain(
      "Near-tie top hits were detected; avoid over-specific species calls without manual review.",
    );
  });

  it("flags BLAST hits in a no-template control", () => {
    const triage = triageBlastOutfmt6(
      "q1\tspeciesA\t99.5\t1450\t0\t0\t1\t1450\t1\t1450\t1e-100\t500\t98",
      "pathogen-id",
    );
    const guards = analyzeDiagnosticTriage(
      { ...DEFAULT_DIAGNOSTIC_CONTEXT, controlRole: "ntc" },
      triage,
    );

    expect(guards).toContainEqual({
      level: "critical",
      message: "Control sample has BLAST hits; review contamination, carryover, and batch validity before interpretation.",
    });
  });

  it("keeps generated runbooks inside an evidence-summary boundary", () => {
    const triage = triageBlastOutfmt6(
      "q1\tspeciesA\t99.5\t1450\t0\t0\t1\t1450\t1\t1450\t1e-100\t500\t98",
      "sixteen-s-its",
    );
    const draft = buildDiagnosticRunbookDraft(
      { ...DEFAULT_DIAGNOSTIC_CONTEXT, workflowId: "sixteen-s-its", sampleId: "S-001" },
      triage,
      [],
    );

    expect(draft).toContain("Sample: S-001");
    expect(draft).toContain("Interpretation boundary: this is an evidence summary, not a diagnostic conclusion.");
  });
});
