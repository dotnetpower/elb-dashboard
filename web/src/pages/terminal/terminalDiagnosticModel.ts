export type DiagnosticWorkflowId =
  | "pathogen-id"
  | "sixteen-s-its"
  | "amr-screen"
  | "primer-specificity"
  | "custom-db-validation";

export type DiagnosticInputType = "fasta" | "fastq" | "contigs" | "primers" | "blast-tabular";
export type ControlRole = "unknown" | "sample" | "positive-control" | "negative-control" | "ntc";
export type DiagnosticGuardLevel = "info" | "warning" | "critical";
export type EvidenceLevel = "none" | "weak" | "review" | "strong";
export type DiagnosticHardeningCategory =
  | "sample"
  | "controls"
  | "input-qc"
  | "database"
  | "command"
  | "blast-triage"
  | "interpretation"
  | "reporting"
  | "operations";
export type DiagnosticHardeningSeverity = "blocker" | "warning" | "advisory";
export type DiagnosticMaturityStatus = "passed" | "current" | "locked";

export interface DiagnosticWorkflow {
  id: DiagnosticWorkflowId;
  label: string;
  summary: string;
  preferredInputs: DiagnosticInputType[];
  preferredDatabases: string[];
  minIdentity: number;
  minCoverage: number;
  recommendedCommands: string[];
  qualityChecks: string[];
  interpretationChecks: string[];
}

export interface DiagnosticSampleContext {
  sampleId: string;
  workflowId: DiagnosticWorkflowId;
  inputType: DiagnosticInputType;
  controlRole: ControlRole;
  database: string;
  organismGroup: string;
}

export interface DiagnosticGuard {
  level: DiagnosticGuardLevel;
  message: string;
}

export interface BlastHit {
  queryId: string;
  subjectId: string;
  identity: number;
  alignmentLength: number;
  evalue: number;
  bitScore: number;
  queryCoverage: number | null;
}

export interface BlastTriage {
  hitCount: number;
  topHit: BlastHit | null;
  ambiguousTopHits: BlastHit[];
  evidenceLevel: EvidenceLevel;
  warnings: string[];
  /** Non-empty, non-comment lines that were skipped (fewer than 12 columns). */
  ignoredLineCount: number;
}

export interface DiagnosticHardeningReviewItem {
  id: string;
  category: DiagnosticHardeningCategory;
  severity: DiagnosticHardeningSeverity;
  label: string;
  rationale: string;
  passed: boolean;
}

export interface DiagnosticMaturityLevelAssessment {
  level: number;
  label: string;
  objective: string;
  critique: string;
  status: DiagnosticMaturityStatus;
  passed: boolean;
  openCriteria: string[];
}

export interface DiagnosticMaturityAssessment {
  currentLevel: number;
  nextLevel: DiagnosticMaturityLevelAssessment | null;
  levels: DiagnosticMaturityLevelAssessment[];
  nextActions: string[];
}

interface DiagnosticHardeningDefinition {
  id: string;
  category: DiagnosticHardeningCategory;
  severity: DiagnosticHardeningSeverity;
  label: string;
  rationale: string;
  evaluate: (input: {
    command: string;
    context: DiagnosticSampleContext;
    triage: BlastTriage;
    workflow: DiagnosticWorkflow;
  }) => boolean;
}

interface DiagnosticMaturityDefinition {
  level: number;
  label: string;
  objective: string;
  critique: string;
  criteria: Array<{
    label: string;
    reviewIds?: string[];
    evaluate?: (input: {
      command: string;
      context: DiagnosticSampleContext;
      triage: BlastTriage;
      review: DiagnosticHardeningReviewItem[];
    }) => boolean;
  }>;
}

export const DIAGNOSTIC_WORKFLOWS: DiagnosticWorkflow[] = [
  {
    id: "pathogen-id",
    label: "Pathogen ID",
    summary: "Screen assembled sequences or query FASTA against a broad reference database.",
    preferredInputs: ["fasta", "contigs", "blast-tabular"],
    preferredDatabases: ["nt", "refseq_genomic", "custom-pathogen-db"],
    minIdentity: 95,
    minCoverage: 80,
    recommendedCommands: [
      "seqkit stats *.fa",
      "blastn -query query.fa -db nt -outfmt '6 qseqid sseqid pident length mismatch gapopen qstart qend sstart send evalue bitscore qcovs' -max_target_seqs 10 -out pathogen_hits.tsv",
    ],
    qualityChecks: ["Confirm sequence count and length distribution.", "Check N content before interpreting weak hits.", "Record the reference database name and version."],
    interpretationChecks: ["Review the top hit and near-tie hits.", "Do not treat a BLAST hit alone as a diagnostic conclusion.", "Check whether the database scope matches the organism group."],
  },
  {
    id: "sixteen-s-its",
    label: "16S / ITS ID",
    summary: "Identify bacterial 16S or fungal ITS amplicons with stricter ambiguity review.",
    preferredInputs: ["fasta", "blast-tabular"],
    preferredDatabases: ["16S_ribosomal_RNA", "ITS_RefSeq_Fungi", "custom-amplicon-db"],
    minIdentity: 98.7,
    minCoverage: 90,
    recommendedCommands: [
      "seqkit stats amplicons.fa",
      "blastn -query amplicons.fa -db 16S_ribosomal_RNA -outfmt '6 qseqid sseqid pident length mismatch gapopen qstart qend sstart send evalue bitscore qcovs' -max_target_seqs 20 -out 16s_hits.tsv",
    ],
    qualityChecks: ["Confirm primer-trimmed amplicon length.", "Check for mixed peaks or multiple top species in the result table.", "Keep positive and negative controls visible in the batch."],
    interpretationChecks: ["Flag species-level calls with close top-hit ties.", "Prefer genus-level wording when identity or coverage is borderline.", "Record the amplicon region and database version."],
  },
  {
    id: "amr-screen",
    label: "AMR Gene Screen",
    summary: "Search contigs or reads for antimicrobial resistance gene evidence.",
    preferredInputs: ["fasta", "contigs", "blast-tabular"],
    preferredDatabases: ["custom-amr-db", "CARD", "ResFinder"],
    minIdentity: 90,
    minCoverage: 80,
    recommendedCommands: [
      "seqkit stats contigs.fa",
      "blastn -query contigs.fa -db custom-amr-db -outfmt '6 qseqid sseqid pident length mismatch gapopen qstart qend sstart send evalue bitscore qcovs' -max_target_seqs 20 -out amr_hits.tsv",
    ],
    qualityChecks: ["Confirm contig/read origin and sample id.", "Check database version and gene naming scheme.", "Inspect partial hits before reporting presence."],
    interpretationChecks: ["Separate gene evidence from phenotype inference.", "Flag partial genes and low-coverage hits.", "Review duplicate or overlapping hits."],
  },
  {
    id: "primer-specificity",
    label: "Primer Specificity",
    summary: "Check primer or probe sequences for off-target similarity.",
    preferredInputs: ["primers", "fasta", "blast-tabular"],
    preferredDatabases: ["nt", "refseq_genomic", "custom-target-panel"],
    minIdentity: 90,
    minCoverage: 80,
    recommendedCommands: [
      "seqkit stats primers.fa",
      "blastn -task blastn-short -query primers.fa -db nt -outfmt '6 qseqid sseqid pident length mismatch gapopen qstart qend sstart send evalue bitscore qcovs' -max_target_seqs 50 -out primer_hits.tsv",
    ],
    qualityChecks: ["Confirm primer orientation and sequence names.", "Use blastn-short for short oligos.", "Review off-target hits near the 3 prime end manually."],
    interpretationChecks: ["Do not clear specificity from BLAST alone.", "Inspect mismatch positions, not only identity.", "Flag broad organism databases separately from panel databases."],
  },
  {
    id: "custom-db-validation",
    label: "Custom DB Validation",
    summary: "Validate a curated FASTA before using it as diagnostic evidence.",
    preferredInputs: ["fasta", "contigs"],
    preferredDatabases: ["custom-db"],
    minIdentity: 95,
    minCoverage: 90,
    recommendedCommands: [
      "seqkit stats refs.fa",
      "seqkit seq -n refs.fa | sort | uniq -d",
      "makeblastdb -in refs.fa -dbtype nucl -parse_seqids -out refs",
    ],
    qualityChecks: ["Check duplicate sequence identifiers.", "Confirm dbtype matches nucleotide or protein content.", "Keep the source FASTA and build date with the DB."],
    interpretationChecks: ["Use a versioned DB name.", "Record source accession policy.", "Run a known positive and negative query before production use."],
  },
];

export const DEFAULT_DIAGNOSTIC_CONTEXT: DiagnosticSampleContext = {
  sampleId: "",
  workflowId: "pathogen-id",
  inputType: "fasta",
  controlRole: "sample",
  database: "nt",
  organismGroup: "Unknown",
};

function hasDeidentifiedSampleId(value: string): boolean {
  const sampleId = value.trim();
  return sampleId.length === 0 || !/@|\b\d{6,}\b/.test(sampleId);
}

function databaseLooksVersioned(value: string): boolean {
  return /\b(v\d|20\d{2}|release|build|rev|version)\b/i.test(value);
}

function commandHasOutfmt6WithCoverage(command: string): boolean {
  return /-outfmt\s+['"]?6\b/.test(command) && /qcovs|qcovhsp/.test(command);
}

function commandHasOption(command: string, option: string): boolean {
  return new RegExp(`${option.replace("-", "\\-")}\\s+\\S+`).test(command);
}

const HARDENING_DEFINITIONS: DiagnosticHardeningDefinition[] = [
  { id: "sample-id-present", category: "sample", severity: "warning", label: "Sample id recorded", rationale: "Runbooks need a stable accession to connect command evidence back to the lab batch.", evaluate: ({ context }) => context.sampleId.trim().length > 0 },
  { id: "sample-id-deidentified", category: "sample", severity: "blocker", label: "Sample id de-identified", rationale: "Terminal notes and copied summaries should not carry direct identifiers.", evaluate: ({ context }) => hasDeidentifiedSampleId(context.sampleId) },
  { id: "organism-group-recorded", category: "sample", severity: "warning", label: "Organism group recorded", rationale: "Expected organism scope changes database choice and interpretation thresholds.", evaluate: ({ context }) => context.organismGroup.trim().length > 0 && context.organismGroup !== "Unknown" },
  { id: "workflow-selected", category: "sample", severity: "advisory", label: "Workflow preset selected", rationale: "A preset makes thresholds and guardrails explicit.", evaluate: ({ workflow }) => Boolean(workflow.id) },
  { id: "input-matches-workflow", category: "sample", severity: "warning", label: "Input type matches workflow", rationale: "Wrong input modality can make an apparently valid hit meaningless.", evaluate: ({ context, workflow }) => workflow.preferredInputs.includes(context.inputType) },
  { id: "control-role-known", category: "controls", severity: "warning", label: "Control role known", rationale: "Controls are the fastest way to detect contamination and failed assays.", evaluate: ({ context }) => context.controlRole !== "unknown" },
  { id: "ntc-no-hits", category: "controls", severity: "blocker", label: "NTC / negative control has no hits", rationale: "Hits in a negative control can invalidate a batch before sample interpretation.", evaluate: ({ context, triage }) => !(context.controlRole === "ntc" || context.controlRole === "negative-control") || triage.hitCount === 0 },
  { id: "positive-control-hits", category: "controls", severity: "blocker", label: "Positive control has evidence", rationale: "A positive control with no evidence suggests assay or command failure.", evaluate: ({ context, triage }) => context.controlRole !== "positive-control" || triage.hitCount > 0 },
  { id: "sample-none-review", category: "controls", severity: "warning", label: "No-hit sample not over-interpreted", rationale: "Absence of BLAST evidence is not equivalent to absence of target without QC context.", evaluate: ({ context, triage }) => context.controlRole !== "sample" || triage.evidenceLevel !== "none" },
  { id: "qc-stats-command", category: "input-qc", severity: "warning", label: "Input stats command available", rationale: "Sequence count and length distribution should be checked before interpretation.", evaluate: ({ command }) => /seqkit\s+stats|fastqc|head\s+|file\s+/.test(command) },
  { id: "fastq-qc", category: "input-qc", severity: "warning", label: "FASTQ quality gate", rationale: "FASTQ needs quality and adapter checks before downstream calls.", evaluate: ({ command, context }) => context.inputType !== "fastq" || /fastqc|seqkit\s+stats/.test(command) },
  { id: "primer-input", category: "input-qc", severity: "warning", label: "Primer/probe input marked", rationale: "Short oligo searches need different BLAST task settings.", evaluate: ({ context }) => context.workflowId !== "primer-specificity" || context.inputType === "primers" },
  { id: "custom-db-duplicate-check", category: "input-qc", severity: "warning", label: "Custom DB duplicate ID check", rationale: "Duplicate FASTA IDs make downstream provenance ambiguous.", evaluate: ({ command, context }) => context.workflowId !== "custom-db-validation" || /uniq\s+-d|seqkit\s+seq\s+-n/.test(command) },
  { id: "database-recorded", category: "database", severity: "warning", label: "Database recorded", rationale: "Results cannot be reviewed without knowing the searched database.", evaluate: ({ context }) => context.database.trim().length > 0 },
  { id: "database-versioned", category: "database", severity: "warning", label: "Database version recorded", rationale: "Diagnostic evidence must survive database drift over time.", evaluate: ({ context }) => databaseLooksVersioned(context.database) || /custom/i.test(context.database) },
  { id: "database-fits-preset", category: "database", severity: "advisory", label: "Database fits workflow preset", rationale: "Unexpected DB choice can be valid, but it needs deliberate justification.", evaluate: ({ context, workflow }) => workflow.preferredDatabases.some((db) => context.database.toLowerCase().includes(db.toLowerCase())) },
  { id: "custom-db-versioned-name", category: "database", severity: "warning", label: "Custom DB name is versioned", rationale: "Custom databases should be immutable or version-labeled.", evaluate: ({ context }) => !/custom/i.test(context.database) || databaseLooksVersioned(context.database) },
  { id: "makeblastdb-dbtype", category: "database", severity: "blocker", label: "makeblastdb declares dbtype", rationale: "Wrong DB type silently breaks nucleotide/protein expectations.", evaluate: ({ command }) => !/makeblastdb\b/.test(command) || /-dbtype\s+(nucl|prot)\b/.test(command) },
  { id: "makeblastdb-parse-seqids", category: "database", severity: "advisory", label: "makeblastdb keeps parseable IDs", rationale: "Traceable IDs make later hit review less brittle.", evaluate: ({ command }) => !/makeblastdb\b/.test(command) || /-parse_seqids\b/.test(command) },
  { id: "blast-has-query", category: "command", severity: "blocker", label: "BLAST query file specified", rationale: "A BLAST command without explicit query is not reviewable.", evaluate: ({ command }) => !/\bblast[npx]?\b/.test(command) || commandHasOption(command, "-query") },
  { id: "blast-has-db", category: "command", severity: "blocker", label: "BLAST database specified", rationale: "A result is meaningless without the target DB.", evaluate: ({ command }) => !/\bblast[npx]?\b/.test(command) || commandHasOption(command, "-db") },
  { id: "blast-tabular-qcovs", category: "command", severity: "warning", label: "BLAST emits tabular qcovs", rationale: "Identity without query coverage is too easy to over-read.", evaluate: ({ command }) => !/\bblast[npx]?\b/.test(command) || commandHasOutfmt6WithCoverage(command) },
  { id: "blast-max-targets", category: "command", severity: "advisory", label: "BLAST captures multiple targets", rationale: "Near-tie hits are invisible if only one target is retained.", evaluate: ({ command }) => !/\bblast[npx]?\b/.test(command) || commandHasOption(command, "-max_target_seqs") },
  { id: "blast-output-file", category: "command", severity: "warning", label: "BLAST writes an output file", rationale: "File outputs support reproducible review and runbook capture.", evaluate: ({ command }) => !/\bblast[npx]?\b/.test(command) || commandHasOption(command, "-out") },
  { id: "primer-blastn-short", category: "command", severity: "warning", label: "Primer search uses blastn-short", rationale: "Default blastn is poorly matched to short oligos.", evaluate: ({ command, context }) => context.workflowId !== "primer-specificity" || !/blastn\b/.test(command) || /-task\s+blastn-short\b/.test(command) },
  { id: "no-destructive-command", category: "command", severity: "blocker", label: "No destructive shell action", rationale: "Diagnostic review should not depend on commands that can delete evidence.", evaluate: ({ command }) => !/(^|\s)(rm\s+-|kubectl\s+delete|az\s+.*\bdelete\b|elastic-blast\s+delete)/.test(command) },
  { id: "top-hit-present", category: "blast-triage", severity: "warning", label: "Top hit available for review", rationale: "The Cockpit cannot triage unpasted or empty BLAST output.", evaluate: ({ triage }) => triage.topHit !== null },
  { id: "identity-threshold", category: "blast-triage", severity: "warning", label: "Top-hit identity meets preset", rationale: "Below-threshold identity should weaken or block species-level language.", evaluate: ({ triage, workflow }) => !triage.topHit || triage.topHit.identity >= workflow.minIdentity },
  { id: "coverage-threshold", category: "blast-triage", severity: "warning", label: "Top-hit coverage meets preset", rationale: "High identity over a short fragment is often misleading.", evaluate: ({ triage, workflow }) => !triage.topHit || (triage.topHit.queryCoverage !== null && triage.topHit.queryCoverage >= workflow.minCoverage) },
  { id: "coverage-present", category: "blast-triage", severity: "warning", label: "Coverage is present", rationale: "Missing qcovs prevents reliable triage.", evaluate: ({ triage }) => !triage.topHit || triage.topHit.queryCoverage !== null },
  { id: "alignment-length-review", category: "blast-triage", severity: "advisory", label: "Alignment length is reviewable", rationale: "Very short alignments need extra biological review.", evaluate: ({ context, triage }) => !triage.topHit || context.workflowId === "primer-specificity" || triage.topHit.alignmentLength >= 80 },
  { id: "ambiguous-hit-review", category: "interpretation", severity: "warning", label: "Near-tie hits resolved", rationale: "Ambiguous top hits should prevent over-specific calls.", evaluate: ({ triage }) => triage.ambiguousTopHits.length === 0 },
  { id: "amr-phenotype-boundary", category: "interpretation", severity: "warning", label: "AMR phenotype boundary respected", rationale: "Gene evidence is not the same as phenotypic resistance.", evaluate: ({ context }) => context.workflowId !== "amr-screen" },
  { id: "primer-specificity-boundary", category: "interpretation", severity: "warning", label: "Primer specificity boundary respected", rationale: "BLAST similarity alone does not clear assay specificity.", evaluate: ({ context }) => context.workflowId !== "primer-specificity" },
  { id: "evidence-not-diagnosis", category: "interpretation", severity: "blocker", label: "Evidence summary, not diagnosis", rationale: "The UI must not turn command output into a regulated diagnostic conclusion.", evaluate: () => true },
  { id: "runbook-sample", category: "reporting", severity: "warning", label: "Runbook can name sample accession", rationale: "Evidence summaries are weaker when sample context is unlabeled.", evaluate: ({ context }) => context.sampleId.trim().length > 0 },
  { id: "runbook-db", category: "reporting", severity: "warning", label: "Runbook can name database", rationale: "A report without DB provenance is not review-ready.", evaluate: ({ context }) => context.database.trim().length > 0 },
  { id: "copy-summary-boundary", category: "reporting", severity: "advisory", label: "Copied summary includes boundary", rationale: "The generated note explicitly remains an evidence summary.", evaluate: () => true },
  { id: "terminal-connected", category: "operations", severity: "advisory", label: "Terminal connection checked", rationale: "Command insertion should only be available when the sidecar is connected.", evaluate: () => true },
  { id: "tool-version-command", category: "operations", severity: "advisory", label: "Tool versions discoverable", rationale: "Version capture improves reproducibility of BLAST and helper utilities.", evaluate: ({ command }) => /elb-tool-versions|blastn\s+-version|azcopy\s+--version/.test(command) },
  { id: "azure-context-command", category: "operations", severity: "advisory", label: "Azure context discoverable", rationale: "Cloud-side runs should capture the selected subscription before mutation.", evaluate: ({ command }) => /az\s+account\s+show/.test(command) || !/\baz\s+/.test(command) },
];

const MATURITY_LEVELS: DiagnosticMaturityDefinition[] = [
  {
    level: 1,
    label: "Raw Shell",
    objective: "A connected terminal can run commands, but the diagnostic intent is not yet structured.",
    critique: "Useful for experts only; the UI cannot tell whether the command, sample, controls, or evidence are review-ready.",
    criteria: [{ label: "Terminal Cockpit is available", evaluate: () => true }],
  },
  {
    level: 2,
    label: "Reviewed Command",
    objective: "The command is visible, normalized, and free of obvious destructive actions.",
    critique: "This prevents the worst shell mistakes, but still says little about laboratory context.",
    criteria: [
      { label: "Command text is present", evaluate: ({ command }) => command.trim().length > 0 },
      { label: "No destructive action is detected", reviewIds: ["no-destructive-command"] },
    ],
  },
  {
    level: 3,
    label: "Sample Context",
    objective: "The run is tied to a de-identified accession, expected organism scope, and matching input type.",
    critique: "A BLAST command without sample context is operational evidence, not a lab-reviewable record.",
    criteria: [
      { label: "Sample id is present and de-identified", reviewIds: ["sample-id-present", "sample-id-deidentified"] },
      { label: "Organism group is recorded", reviewIds: ["organism-group-recorded"] },
      { label: "Input type matches the selected workflow", reviewIds: ["input-matches-workflow"] },
    ],
  },
  {
    level: 4,
    label: "Control Awareness",
    objective: "The Cockpit knows whether it is reviewing a sample, positive control, negative control, or NTC.",
    critique: "Without control role and control-specific triage, contamination and assay failure can look like sample evidence.",
    criteria: [
      { label: "Control role is known", reviewIds: ["control-role-known"] },
      { label: "Negative controls are clean", reviewIds: ["ntc-no-hits"] },
      { label: "Positive controls have expected evidence", reviewIds: ["positive-control-hits"] },
    ],
  },
  {
    level: 5,
    label: "Input QC Gate",
    objective: "The workflow includes sequence/file quality checks before interpretation.",
    critique: "High-scoring hits can still be misleading when the input quality, primer mode, or custom FASTA hygiene is unknown.",
    criteria: [
      { label: "Input statistics command is available", reviewIds: ["qc-stats-command"] },
      { label: "FASTQ and primer-specific gates are satisfied", reviewIds: ["fastq-qc", "primer-input"] },
      { label: "Custom DB duplicate identifiers are checked when relevant", reviewIds: ["custom-db-duplicate-check"] },
    ],
  },
  {
    level: 6,
    label: "Database Provenance",
    objective: "The searched database is named, versioned, and compatible with the selected workflow.",
    critique: "Unversioned database evidence decays over time and cannot be reproduced in review.",
    criteria: [
      { label: "Database name and version are recorded", reviewIds: ["database-recorded", "database-versioned"] },
      { label: "Database fits the workflow or is deliberately custom", reviewIds: ["database-fits-preset", "custom-db-versioned-name"] },
      { label: "Custom BLAST DB build command is typed correctly", reviewIds: ["makeblastdb-dbtype"] },
    ],
  },
  {
    level: 7,
    label: "Reviewable BLAST Command",
    objective: "BLAST commands preserve query, database, coverage, multiple targets, and output file evidence.",
    critique: "Identity-only or single-target output is not enough for diagnostic triage.",
    criteria: [
      { label: "A BLAST command is being reviewed", evaluate: ({ command }) => /\bblast[npx]?\b/.test(command) },
      { label: "Query and database are explicit", reviewIds: ["blast-has-query", "blast-has-db"] },
      { label: "Tabular output includes query coverage", reviewIds: ["blast-tabular-qcovs"] },
      { label: "Multiple targets and output file are captured", reviewIds: ["blast-max-targets", "blast-output-file"] },
    ],
  },
  {
    level: 8,
    label: "Evidence Triage",
    objective: "Pasted BLAST output has a top hit, coverage, threshold checks, and ambiguity review.",
    critique: "Until real result rows are pasted, the Cockpit can validate setup but cannot validate evidence.",
    criteria: [
      { label: "Top hit is available", reviewIds: ["top-hit-present"] },
      { label: "Identity and coverage meet preset thresholds", reviewIds: ["identity-threshold", "coverage-threshold", "coverage-present"] },
      { label: "Near-tie hits and short alignments are reviewed", reviewIds: ["ambiguous-hit-review", "alignment-length-review"] },
    ],
  },
  {
    level: 9,
    label: "Interpretation Boundary",
    objective: "The output stays inside evidence-summary language and respects assay-specific limits.",
    critique: "This level keeps the tool useful to researchers without pretending to make regulated diagnostic conclusions.",
    criteria: [
      { label: "Evidence is not presented as diagnosis", reviewIds: ["evidence-not-diagnosis", "copy-summary-boundary"] },
      { label: "AMR and primer-specificity limits are respected", reviewIds: ["amr-phenotype-boundary", "primer-specificity-boundary"] },
      { label: "Runbook can name sample and database", reviewIds: ["runbook-sample", "runbook-db"] },
    ],
  },
  {
    level: 10,
    label: "Audit-Ready Evidence Package",
    objective: "The command, context, controls, database, evidence, and reproducibility metadata are all ready for handoff.",
    critique: "Level 10 is intentionally strict: no open blockers or warnings, strong evidence, and reproducibility commands visible.",
    criteria: [
      { label: "No blocker or warning checks remain", evaluate: ({ review }) => review.every((item) => item.passed || item.severity === "advisory") },
      { label: "Evidence strength is strong", evaluate: ({ triage }) => triage.evidenceLevel === "strong" },
      { label: "Tool and cloud context are discoverable", reviewIds: ["tool-version-command", "azure-context-command"] },
    ],
  },
];

export function getDiagnosticWorkflow(id: DiagnosticWorkflowId): DiagnosticWorkflow {
  return DIAGNOSTIC_WORKFLOWS.find((workflow) => workflow.id === id) ?? DIAGNOSTIC_WORKFLOWS[0];
}

export function getDiagnosticHardeningCheckCount(): number {
  return HARDENING_DEFINITIONS.length;
}

export function evaluateDiagnosticHardeningReview(
  command: string,
  context: DiagnosticSampleContext,
  triage: BlastTriage,
): DiagnosticHardeningReviewItem[] {
  const workflow = getDiagnosticWorkflow(context.workflowId);
  return HARDENING_DEFINITIONS.map((definition) => ({
    id: definition.id,
    category: definition.category,
    severity: definition.severity,
    label: definition.label,
    rationale: definition.rationale,
    passed: definition.evaluate({ command, context, triage, workflow }),
  }));
}

function areReviewChecksPassed(review: DiagnosticHardeningReviewItem[], ids: string[]): boolean {
  return ids.every((id) => review.find((item) => item.id === id)?.passed === true);
}

export function evaluateDiagnosticMaturity(
  command: string,
  context: DiagnosticSampleContext,
  triage: BlastTriage,
): DiagnosticMaturityAssessment {
  const review = evaluateDiagnosticHardeningReview(command, context, triage);
  let highestSequentialLevel = 0;

  const rawLevels = MATURITY_LEVELS.map((definition) => {
    const openCriteria = definition.criteria
      .filter((criterion) => {
        if (criterion.reviewIds) return !areReviewChecksPassed(review, criterion.reviewIds);
        return criterion.evaluate?.({ command, context, triage, review }) === false;
      })
      .map((criterion) => criterion.label);
    const passed = openCriteria.length === 0;
    if (passed && highestSequentialLevel === definition.level - 1) {
      highestSequentialLevel = definition.level;
    }
    return { definition, openCriteria, passed };
  });

  const levels = rawLevels.map(({ definition, openCriteria, passed }) => ({
    level: definition.level,
    label: definition.label,
    objective: definition.objective,
    critique: definition.critique,
    status: passed && definition.level <= highestSequentialLevel
      ? "passed" as const
      : definition.level === highestSequentialLevel + 1
        ? "current" as const
        : "locked" as const,
    passed,
    openCriteria,
  }));
  const nextLevel = levels.find((level) => level.status === "current") ?? null;

  return {
    currentLevel: highestSequentialLevel,
    nextLevel,
    levels,
    nextActions: nextLevel?.openCriteria.slice(0, 3) ?? [],
  };
}

export function analyzeDiagnosticReadiness(
  command: string,
  context: DiagnosticSampleContext,
): DiagnosticGuard[] {
  const workflow = getDiagnosticWorkflow(context.workflowId);
  const normalized = command.trim();
  const guards: DiagnosticGuard[] = [];

  if (!context.sampleId.trim()) {
    guards.push({ level: "warning", message: "Add a sample id before capturing a runbook or report note." });
  } else if (/@|\b\d{6,}\b/.test(context.sampleId)) {
    guards.push({ level: "critical", message: "Sample id may contain identifying data; use a de-identified lab accession." });
  }
  if (context.controlRole === "unknown") {
    guards.push({ level: "warning", message: "Mark whether this is a sample, positive control, negative control, or NTC." });
  }
  if (!workflow.preferredInputs.includes(context.inputType)) {
    guards.push({ level: "warning", message: `${workflow.label} usually expects ${workflow.preferredInputs.join(" or ")} input.` });
  }
  if (context.database && !workflow.preferredDatabases.some((db) => context.database.toLowerCase().includes(db.toLowerCase()))) {
    guards.push({ level: "info", message: "The selected database is outside the usual preset list; record why it is appropriate." });
  }
  if (/blastn\b/.test(normalized) && !/-outfmt\s+['"]?6\b/.test(normalized)) {
    guards.push({ level: "warning", message: "Use tabular outfmt 6 with qcovs so the Cockpit can triage identity and coverage." });
  }
  if (context.workflowId === "primer-specificity" && /blastn\b/.test(normalized) && !/-task\s+blastn-short\b/.test(normalized)) {
    guards.push({ level: "warning", message: "Primer/probe checks should usually use -task blastn-short." });
  }
  if (/makeblastdb\b/.test(normalized) && !/-parse_seqids\b/.test(normalized)) {
    guards.push({ level: "info", message: "Consider -parse_seqids so sequence identifiers remain traceable." });
  }
  if (/makeblastdb\b/.test(normalized) && !/-dbtype\s+(nucl|prot)\b/.test(normalized)) {
    guards.push({ level: "critical", message: "makeblastdb needs an explicit -dbtype nucl or -dbtype prot." });
  }
  if (/(^|\s)(blastp|blastx)\b/.test(normalized) && context.inputType !== "contigs") {
    guards.push({ level: "warning", message: "Protein search selected; confirm the input has been translated or is protein sequence." });
  }
  if (/\b(nt|nr|refseq|CARD|ResFinder)\b/i.test(context.database) && !/\b(v\d|20\d{2}|release)\b/i.test(context.database)) {
    guards.push({ level: "info", message: "Use a versioned database label in notes when possible." });
  }

  return guards;
}

export function analyzeDiagnosticTriage(
  context: DiagnosticSampleContext,
  triage: BlastTriage,
): DiagnosticGuard[] {
  const guards: DiagnosticGuard[] = [];

  if ((context.controlRole === "negative-control" || context.controlRole === "ntc") && triage.hitCount > 0) {
    guards.push({ level: "critical", message: "Control sample has BLAST hits; review contamination, carryover, and batch validity before interpretation." });
  }
  if (context.controlRole === "positive-control" && triage.hitCount === 0) {
    guards.push({ level: "critical", message: "Positive control has no BLAST hits; verify assay input, database, and command parameters." });
  }
  if (context.controlRole === "sample" && triage.evidenceLevel === "none") {
    guards.push({ level: "warning", message: "No evidence detected for this sample; confirm input quality before treating this as absence." });
  }
  if (triage.ambiguousTopHits.length > 0) {
    guards.push({ level: "warning", message: "Ambiguous top hits require manual taxonomic review before species-level wording." });
  }

  return guards;
}

function parseNumber(value: string): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function parseBlastOutfmt6(text: string): BlastHit[] {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"))
    .map((line) => line.split(/\t|\s{2,}/))
    .filter((fields) => fields.length >= 12)
    .map((fields) => {
      const identity = parseNumber(fields[2]) ?? 0;
      const alignmentLength = parseNumber(fields[3]) ?? 0;
      const evalue = parseNumber(fields[10]) ?? Number.POSITIVE_INFINITY;
      const bitScore = parseNumber(fields[11]) ?? 0;
      const queryCoverage = fields[12] ? parseNumber(fields[12]) : null;
      return {
        queryId: fields[0],
        subjectId: fields[1],
        identity,
        alignmentLength,
        evalue,
        bitScore,
        queryCoverage,
      };
    })
    .sort((left, right) => right.bitScore - left.bitScore || left.evalue - right.evalue);
}

/**
 * Count non-empty, non-comment lines that were dropped because they had fewer
 * than the 12 columns a BLAST outfmt 6 row needs. Surfacing this lets the
 * cockpit explain a "0 hits" paste instead of silently discarding bad input.
 */
export function countIgnoredOutfmt6Lines(text: string): number {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"))
    .map((line) => line.split(/\t|\s{2,}/))
    .filter((fields) => fields.length < 12).length;
}

export function triageBlastOutfmt6(
  text: string,
  workflowId: DiagnosticWorkflowId,
): BlastTriage {
  const workflow = getDiagnosticWorkflow(workflowId);
  const hits = parseBlastOutfmt6(text);
  const ignoredLineCount = countIgnoredOutfmt6Lines(text);
  const topHit = hits[0] ?? null;
  const warnings: string[] = [];

  if (!topHit) {
    return {
      hitCount: 0,
      topHit: null,
      ambiguousTopHits: [],
      evidenceLevel: "none",
      warnings: ignoredLineCount > 0
        ? [`No valid outfmt 6 rows; ${ignoredLineCount} line(s) had fewer than 12 columns.`]
        : ["No tabular BLAST hits were detected."],
      ignoredLineCount,
    };
  }

  const ambiguousTopHits = hits
    .slice(1)
    .filter((hit) => hit.subjectId !== topHit.subjectId && hit.bitScore >= topHit.bitScore * 0.99)
    .slice(0, 5);

  if (topHit.identity < workflow.minIdentity) {
    warnings.push(`Top-hit identity is below the ${workflow.label} preset threshold (${workflow.minIdentity}%).`);
  }
  if (topHit.queryCoverage === null) {
    warnings.push("Query coverage is missing; include qcovs in outfmt 6 for stronger triage.");
  } else if (topHit.queryCoverage < workflow.minCoverage) {
    warnings.push(`Top-hit query coverage is below the preset threshold (${workflow.minCoverage}%).`);
  }
  if (topHit.alignmentLength < 80 && workflowId !== "primer-specificity") {
    warnings.push("Alignment length is short; review whether the match is biologically meaningful.");
  }
  if (ambiguousTopHits.length > 0) {
    warnings.push("Near-tie top hits were detected; avoid over-specific species calls without manual review.");
  }
  if (ignoredLineCount > 0) {
    warnings.push(`${ignoredLineCount} line(s) were ignored because they had fewer than 12 columns.`);
  }

  const passesIdentity = topHit.identity >= workflow.minIdentity;
  const passesCoverage = topHit.queryCoverage !== null && topHit.queryCoverage >= workflow.minCoverage;
  const evidenceLevel: EvidenceLevel = warnings.length === 0 && passesIdentity && passesCoverage
    ? "strong"
    : passesIdentity && (passesCoverage || topHit.queryCoverage === null)
      ? "review"
      : "weak";

  return {
    hitCount: hits.length,
    topHit,
    ambiguousTopHits,
    evidenceLevel,
    warnings,
    ignoredLineCount,
  };
}

export function buildDiagnosticRunbookDraft(
  context: DiagnosticSampleContext,
  triage: BlastTriage,
  guards: DiagnosticGuard[],
): string {
  const workflow = getDiagnosticWorkflow(context.workflowId);
  const topHitLine = triage.topHit
    ? `Top hit: ${triage.topHit.subjectId} (${triage.topHit.identity}% identity, ${triage.topHit.queryCoverage ?? "unknown"}% qcovs, bitscore ${triage.topHit.bitScore})`
    : "Top hit: none detected";
  const guardLines = guards.length
    ? guards.map((guard) => `- ${guard.level.toUpperCase()}: ${guard.message}`).join("\n")
    : "- No cockpit guard warnings.";
  const warningLines = triage.warnings.length
    ? triage.warnings.map((warning) => `- ${warning}`).join("\n")
    : "- No triage warnings.";

  return [
    `Workflow: ${workflow.label}`,
    `Sample: ${context.sampleId || "unlabeled"}`,
    `Control role: ${context.controlRole}`,
    `Input type: ${context.inputType}`,
    `Database: ${context.database || "not recorded"}`,
    `Organism group: ${context.organismGroup || "not recorded"}`,
    `Evidence level: ${triage.evidenceLevel}`,
    topHitLine,
    "",
    "Cockpit guards:",
    guardLines,
    "",
    "BLAST triage notes:",
    warningLines,
    "",
    "Interpretation boundary: this is an evidence summary, not a diagnostic conclusion.",
  ].join("\n");
}
