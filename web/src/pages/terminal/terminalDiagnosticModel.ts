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

export function getDiagnosticWorkflow(id: DiagnosticWorkflowId): DiagnosticWorkflow {
  return DIAGNOSTIC_WORKFLOWS.find((workflow) => workflow.id === id) ?? DIAGNOSTIC_WORKFLOWS[0];
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

export function triageBlastOutfmt6(
  text: string,
  workflowId: DiagnosticWorkflowId,
): BlastTriage {
  const workflow = getDiagnosticWorkflow(workflowId);
  const hits = parseBlastOutfmt6(text);
  const topHit = hits[0] ?? null;
  const warnings: string[] = [];

  if (!topHit) {
    return {
      hitCount: 0,
      topHit: null,
      ambiguousTopHits: [],
      evidenceLevel: "none",
      warnings: ["No tabular BLAST hits were detected."],
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
