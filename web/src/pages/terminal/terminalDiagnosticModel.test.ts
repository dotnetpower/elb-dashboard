import { describe, expect, it } from "vitest";

import {
  analyzeDiagnosticReadiness,
  analyzeDiagnosticTriage,
  buildDiagnosticRunbookDraft,
  countIgnoredOutfmt6Lines,
  DEFAULT_DIAGNOSTIC_CONTEXT,
  DIAGNOSTIC_WORKFLOWS,
  evaluateDiagnosticHardeningReview,
  evaluateDiagnosticMaturity,
  getDiagnosticHardeningCheckCount,
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

  it("counts malformed outfmt 6 lines and surfaces them in triage", () => {
    const text = [
      "q1\tspeciesA\t99.5\t1450\t0\t0\t1\t1450\t1\t1450\t1e-100\t500\t98",
      "garbage line without enough columns",
      "# a comment line is ignored silently",
      "",
    ].join("\n");

    expect(countIgnoredOutfmt6Lines(text)).toBe(1);

    const triage = triageBlastOutfmt6(text, "pathogen-id");
    expect(triage.ignoredLineCount).toBe(1);
    expect(triage.warnings.some((warning) => warning.includes("ignored"))).toBe(true);
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

  it("keeps at least thirty cold-review hardening checks active", () => {
    expect(getDiagnosticHardeningCheckCount()).toBeGreaterThanOrEqual(30);
  });

  it("turns the cold-review critique into actionable failed checks", () => {
    const triage = triageBlastOutfmt6("", "pathogen-id");
    const review = evaluateDiagnosticHardeningReview(
      "blastn -query query.fa -db nt -out hits.tsv",
      DEFAULT_DIAGNOSTIC_CONTEXT,
      triage,
    );

    expect(review.length).toBeGreaterThanOrEqual(30);
    expect(review.filter((item) => !item.passed).map((item) => item.id)).toEqual(
      expect.arrayContaining([
        "sample-id-present",
        "organism-group-recorded",
        "database-versioned",
        "blast-tabular-qcovs",
        "top-hit-present",
      ]),
    );
    expect(review).toContainEqual(
      expect.objectContaining({ id: "sample-id-deidentified", passed: true }),
    );
  });

  it("marks identifying sample ids and negative-control hits as blockers", () => {
    const triage = triageBlastOutfmt6(
      "q1\tspeciesA\t99.5\t1450\t0\t0\t1\t1450\t1\t1450\t1e-100\t500\t98",
      "pathogen-id",
    );
    const review = evaluateDiagnosticHardeningReview(
      "blastn -query query.fa -db nt -outfmt '6 qseqid sseqid pident length mismatch gapopen qstart qend sstart send evalue bitscore qcovs' -max_target_seqs 10 -out hits.tsv",
      { ...DEFAULT_DIAGNOSTIC_CONTEXT, sampleId: "patient-12345678", controlRole: "negative-control" },
      triage,
    );

    expect(review).toContainEqual(
      expect.objectContaining({ id: "sample-id-deidentified", severity: "blocker", passed: false }),
    );
    expect(review).toContainEqual(
      expect.objectContaining({ id: "ntc-no-hits", severity: "blocker", passed: false }),
    );
  });

  it("stages maturity as a ten-level ladder instead of jumping to audit-ready", () => {
    const maturity = evaluateDiagnosticMaturity(
      "az account show -o table",
      DEFAULT_DIAGNOSTIC_CONTEXT,
      triageBlastOutfmt6("", "pathogen-id"),
    );

    expect(maturity.levels).toHaveLength(10);
    expect(maturity.currentLevel).toBe(2);
    expect(maturity.nextLevel).toMatchObject({ level: 3, label: "Sample Context" });
    expect(maturity.nextActions).toEqual(
      expect.arrayContaining(["Sample id is present and de-identified"]),
    );
  });

  it("does not let a QC-only command pass the reviewable BLAST command level", () => {
    const maturity = evaluateDiagnosticMaturity(
      "seqkit stats query.fa",
      {
        ...DEFAULT_DIAGNOSTIC_CONTEXT,
        sampleId: "S-001",
        organismGroup: "Respiratory bacteria",
        database: "nt release 2026-05",
      },
      triageBlastOutfmt6("", "pathogen-id"),
    );

    expect(maturity.currentLevel).toBe(6);
    expect(maturity.nextLevel).toMatchObject({ level: 7, label: "Reviewable BLAST Command" });
    expect(maturity.nextActions).toContain("A BLAST command is being reviewed");
  });

  it("reaches level ten only with context, QC, database provenance, triage, and reproducibility metadata", () => {
    const command = [
      "seqkit stats query.fa",
      "blastn -version",
      "az account show -o table",
      "blastn -query query.fa -db nt -outfmt '6 qseqid sseqid pident length mismatch gapopen qstart qend sstart send evalue bitscore qcovs' -max_target_seqs 10 -out hits.tsv",
    ].join(" && ");
    const triage = triageBlastOutfmt6(
      "q1\tspeciesA\t99.5\t1450\t0\t0\t1\t1450\t1\t1450\t1e-100\t500\t98",
      "pathogen-id",
    );
    const maturity = evaluateDiagnosticMaturity(
      command,
      {
        ...DEFAULT_DIAGNOSTIC_CONTEXT,
        sampleId: "S-001",
        organismGroup: "Respiratory bacteria",
        database: "nt release 2026-05",
      },
      triage,
    );

    expect(maturity.currentLevel).toBe(10);
    expect(maturity.nextLevel).toBeNull();
    expect(maturity.nextActions).toEqual([]);
  });
});
