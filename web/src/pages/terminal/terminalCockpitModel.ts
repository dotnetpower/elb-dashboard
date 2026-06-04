import {
  Activity,
  AlertTriangle,
  BookMarked,
  Braces,
  BrainCircuit,
  CheckCircle2,
  ClipboardCheck,
  CloudCog,
  Code2,
  Coins,
  Compass,
  Database,
  FileCode2,
  FileDiff,
  FileSearch,
  FileStack,
  FolderTree,
  Gauge,
  GitCompareArrows,
  History,
  KeyRound,
  LifeBuoy,
  ListChecks,
  LockKeyhole,
  Network,
  PackageCheck,
  PlayCircle,
  RotateCcw,
  SearchCheck,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  SplitSquareHorizontal,
  TerminalSquare,
  TimerReset,
  Undo2,
  Wand2,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

export type CommandImpact = "local-read" | "local-write" | "azure-read" | "azure-write" | "kubernetes" | "destructive" | "unknown";
export type CommandRisk = "low" | "medium" | "high";

export type CommandConfidence = "high" | "medium" | "low";

export interface CommandAnalysis {
  impact: CommandImpact;
  risk: CommandRisk;
  summary: string;
  checks: string[];
  rollback: string | null;
  saferCommand: string | null;
  /**
   * How sure the classifier is about this verdict. "high" = a single
   * recognised command, "medium" = a compound/piped command whose worst
   * segment was used, "low" = the impact could not be recognised.
   */
  confidence: CommandConfidence;
}

export interface CockpitWorkflow {
  id: string;
  label: string;
  command: string;
  intent: string;
  impact: CommandImpact;
}

export interface CockpitChapter {
  id: string;
  label: string;
  status: "ready" | "active" | "next";
  detail: string;
}

// Honest two-tier classification. "shipped" means the capability is backed by a
// real, user-reachable mechanism today (a Cockpit panel, a terminal guard, or a
// CLI helper). "roadmap" means the idea is designed but not yet wired to a
// verifiable surface — it must never be presented as if it already works. The
// previous three-tier "live | guarded | foundation" labelling inflated the
// "live" count with marketing-only entries; collapsing it removes that gap.
export type CapabilityTier = "shipped" | "roadmap";

export interface InnovationCapability {
  id: string;
  label: string;
  tier: CapabilityTier;
  icon: LucideIcon;
}

// Fields backing the Cockpit "Config Builder" form. The builder composes an
// `elb-cfg` invocation (the terminal-side helper is the single source of truth
// for INI layout) rather than rendering INI client-side, so the form can never
// drift from api/services/blast/config.py.
export interface ElbCfgFormFields {
  program: string;
  db: string;
  queries: string;
  results: string;
  machineType: string;
  numNodes: string;
  region: string;
  resourceGroup: string;
  storageAccount: string;
  acrName: string;
  output: string;
}

export const ELB_CFG_FORM_DEFAULTS: ElbCfgFormFields = {
  program: "blastn",
  db: "",
  queries: "",
  results: "",
  machineType: "",
  numNodes: "",
  region: "",
  resourceGroup: "",
  storageAccount: "",
  acrName: "",
  output: "~/elastic-blast.ini",
};

// Single-quote a value for safe POSIX shell embedding. Only values that contain
// shell-significant characters are quoted, to keep the generated command
// readable for the common simple case.
function shellQuote(value: string): string {
  if (/^[A-Za-z0-9_./:@~-]+$/.test(value)) return value;
  return `'${value.replace(/'/g, "'\\''")}'`;
}

/**
 * Build an `elb-cfg` command line from the form fields. Empty fields are
 * omitted so the helper falls back to its environment-seeded defaults. The
 * required research fields (db / queries / results) are always passed when set;
 * the command is intentionally a single line so it flows through the existing
 * Command Preview → risk classification → Insert pipeline.
 */
export function buildElbCfgCommand(fields: ElbCfgFormFields): string {
  const parts = ["elb-cfg"];
  const push = (flag: string, value: string) => {
    const v = value.trim();
    if (v) {
      parts.push(flag, shellQuote(v));
    }
  };
  push("--program", fields.program);
  push("--db", fields.db);
  push("--queries", fields.queries);
  push("--results", fields.results);
  push("--machine-type", fields.machineType);
  push("--num-nodes", fields.numNodes);
  push("--region", fields.region);
  push("--rg", fields.resourceGroup);
  push("--storage-account", fields.storageAccount);
  push("--acr-name", fields.acrName);
  const output = fields.output.trim();
  if (output) {
    parts.push("-o", shellQuote(output));
  }
  return parts.join(" ");
}


const DESTRUCTIVE_PATTERNS = [
  /(^|\s)rm\s+(-[rRfF]*\s*)?\//,
  /(^|\s)rm\s+.*(-r|-rf|-fr)/,
  /kubectl\s+delete\b/,
  /az\s+.*\bdelete\b/,
  /elastic-blast\s+delete\b/,
  /\bsudo\s+rm\b/,
  />\s*\/dev\/sd[a-z]/,
];

const AZURE_WRITE_PATTERNS = [
  /az\s+.*\b(create|update|set|start|stop|restart|deploy|build)\b/,
  /azcopy\s+(copy|sync|remove)\b/,
  /elastic-blast\s+submit\b/,
];

const KUBERNETES_PATTERNS = [/kubectl\s+/, /helm\s+/, /kustomize\s+/];
// Real file redirections only: a `>`/`>>` that is NOT an fd duplication
// (`2>&1`, `&>`, `1>&2`). The char immediately before `>` must not be a digit
// or `&`, which rules out `2>&1` / `&>>file` while still catching `cmd > out`.
const FILE_REDIRECT_PATTERN = /(^|[^0-9&>])>>?\s*[^&|>\s]/;
const LOCAL_WRITE_PATTERNS = [
  FILE_REDIRECT_PATTERN,
  /\b(mkdir|cp|mv|touch|nano|vim|makeblastdb|pigz|tar\s+-x|unzip)\b/,
];
const AZURE_READ_PATTERNS = [/az\s+.*\b(list|show|get)\b/, /azcopy\s+list\b/];
// Read-only Kubernetes / ElasticBLAST verbs: inspecting state, not mutating it.
const KUBERNETES_READ_PATTERNS = [
  /kubectl\s+(get|describe|logs|top|explain|api-resources|config\s+(get|view|current-context))\b/,
  /helm\s+(list|status|get|history|show)\b/,
];
// No-op / inspection shell builtins and utilities that are always safe to run.
const NOOP_PATTERNS = [
  /^(echo|printf|cd|clear|history|export|env|printenv|set|alias|unalias|whoami|id|date|uptime|hostname|df|du|free|uname|true|false|type|help|man|exit|logout|:)\b/,
  /^(elb-tool-versions|tool-versions)\b/,
];
const LOCAL_READ_PATTERNS = [/\b(pwd|ls|cat|less|head|tail|file|tree|seqkit\s+stats|blastn\s+-version|which|command\s+-v|grep|wc|sort|uniq|diff|stat|find)\b/];

function matchesAny(command: string, patterns: RegExp[]): boolean {
  return patterns.some((pattern) => pattern.test(command));
}

// Replace the contents of single/double quoted string literals with spaces so
// shell-significant characters that live INSIDE a quoted argument (`>` in
// `echo "a > b"`, `&&` in `awk 'NF && x'`, `rm -rf` in `grep "rm -rf"`) are not
// mistaken for redirections, operators, or destructive verbs. Quote delimiters
// are dropped; unterminated quotes consume to the end of the string.
function stripQuoted(input: string): string {
  let out = "";
  let quote: '"' | "'" | null = null;
  for (const ch of input) {
    if (quote) {
      if (ch === quote) quote = null;
      else out += " ";
    } else if (ch === '"' || ch === "'") {
      quote = ch;
    } else {
      out += ch;
    }
  }
  return out;
}

// Drop a leading run of `VAR=value` environment assignments (`FOO=bar echo hi`)
// so the real command keyword reaches the anchored NOOP/builtin patterns.
function stripEnvAssignmentPrefix(input: string): string {
  return input.replace(
    /^(?:[A-Za-z_][A-Za-z0-9_]*=(?:[^\s'"]*|'[^']*'|"[^"]*")\s+)+/,
    "",
  );
}

// Split a compound command into the segments the shell would run, honouring
// single/double quotes so operators inside a quoted argument do not create
// bogus segments. Splits on `&&`, `||`, `|`, `;`, and newlines only.
function splitShellSegments(input: string): string[] {
  const segments: string[] = [];
  let current = "";
  let quote: '"' | "'" | null = null;
  for (let i = 0; i < input.length; i += 1) {
    const ch = input[i];
    if (quote) {
      current += ch;
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      current += ch;
      continue;
    }
    if (ch === "&" && input[i + 1] === "&") {
      segments.push(current);
      current = "";
      i += 1;
      continue;
    }
    if (ch === "|" && input[i + 1] === "|") {
      segments.push(current);
      current = "";
      i += 1;
      continue;
    }
    if (ch === "|" || ch === ";" || ch === "\n") {
      segments.push(current);
      current = "";
      continue;
    }
    current += ch;
  }
  segments.push(current);
  return segments.map((segment) => segment.trim()).filter((segment) => segment.length > 0);
}

// Turn a `kubectl delete ...` invocation into a non-destructive `kubectl get`
// preview by swapping the verb and dropping delete-only flags that would make
// the `get` form invalid (`--grace-period`, `--force`, `--now`, `--cascade`,
// `--wait`). Returns null when the segment is not a kubectl delete.
function buildKubectlDeleteSafer(segment: string): string | null {
  if (!/^kubectl\s+delete\b/.test(segment)) return null;
  return segment
    .replace(/^kubectl\s+delete\b/, "kubectl get")
    .replace(/\s--grace-period(?:=\S+|\s+\S+)?/g, "")
    .replace(/\s--force\b/g, "")
    .replace(/\s--now\b/g, "")
    .replace(/\s--cascade(?:=\S+|\s+\S+)?/g, "")
    .replace(/\s--wait(?:=\S+)?\b/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

export function classifyCommand(command: string): CommandAnalysis {
  const normalized = command.trim();
  if (!normalized) {
    return {
      impact: "unknown",
      risk: "low",
      summary: "Waiting for a command to inspect.",
      checks: ["Type or paste a command first."],
      rollback: null,
      saferCommand: null,
      confidence: "low",
    };
  }

  // Split a compound command into the segments the shell would run, so the
  // verdict reflects the worst part rather than whatever the first regex
  // happens to match. `az group list | grep delete` is then read-only, while
  // `ls && rm -rf /` is correctly flagged destructive. The split is
  // quote-aware so operators inside a quoted argument do not create bogus
  // segments.
  const segments = splitShellSegments(normalized);
  const parts = segments.length > 0 ? segments : [normalized];

  const RISK_RANK: Record<CommandRisk, number> = { low: 0, medium: 1, high: 2 };
  let worst = classifySegment(parts[0]);
  for (const segment of parts.slice(1)) {
    const candidate = classifySegment(segment);
    if (RISK_RANK[candidate.risk] > RISK_RANK[worst.risk]) {
      worst = candidate;
    }
  }

  // A compound command can only ever be judged from its worst segment, so it
  // never earns "high" confidence. If that worst segment was itself
  // unrecognised ("low"), the compound stays "low"; otherwise it is "medium".
  if (parts.length > 1) {
    const confidence: CommandConfidence = worst.confidence === "low" ? "low" : "medium";
    return { ...worst, confidence };
  }
  return worst;
}

function classifySegment(segment: string): CommandAnalysis {
  // `cleaned` keeps the real tokens (for building safer-command suggestions);
  // `probe` additionally blanks quoted string contents so pattern matching
  // never trips on shell-significant characters inside a string literal.
  const cleaned = stripEnvAssignmentPrefix(segment.trim());
  const normalized = stripQuoted(cleaned);
  if (matchesAny(normalized, DESTRUCTIVE_PATTERNS)) {
    return {
      impact: "destructive",
      risk: "high",
      summary: "This can delete or mutate resources and should not run without an explicit recovery plan.",
      checks: ["Confirm the target path or resource name.", "Snapshot important files first.", "Prefer listing targets before deletion."],
      rollback: "Rollback may require restoring from backups or recreating cloud resources.",
      saferCommand: buildKubectlDeleteSafer(cleaned),
      confidence: "high",
    };
  }

  if (matchesAny(normalized, KUBERNETES_READ_PATTERNS)) {
    return {
      impact: "kubernetes",
      risk: "medium",
      summary: "This reads Kubernetes state from the terminal sidecar's active context without mutation.",
      checks: ["Check kubectl config current-context.", "Confirm namespace and cluster."],
      rollback: null,
      saferCommand: null,
      confidence: "high",
    };
  }

  if (matchesAny(normalized, KUBERNETES_PATTERNS)) {
    return {
      impact: "kubernetes",
      risk: matchesAny(normalized, [/\b(apply|scale|rollout|delete)\b/]) ? "high" : "medium",
      summary: "This talks to the active Kubernetes context from the terminal sidecar.",
      checks: ["Check kubectl config current-context.", "Confirm namespace and cluster.", "Prefer read-only get/describe before mutation."],
      rollback: "Use the previous manifest, scale value, or job state if the command changes cluster state.",
      saferCommand: normalized.includes("kubectl get") ? null : "kubectl get pods -A",
      confidence: "high",
    };
  }

  // `az account set` / `az configure` switch local CLI config (active
  // subscription, defaults) rather than mutating cloud resources, so they are
  // read-class context changes, not azure-write.
  if (/^az\s+(account\s+set|configure)\b/.test(normalized)) {
    return {
      impact: "azure-read",
      risk: "medium",
      summary: "This changes the local Azure CLI context (active subscription or defaults), not cloud resources.",
      checks: ["Confirm the subscription or default you are switching to.", "Run az account show -o table afterwards."],
      rollback: "Re-run az account set with the previous subscription if needed.",
      saferCommand: "az account show -o table",
      confidence: "high",
    };
  }

  if (matchesAny(normalized, AZURE_WRITE_PATTERNS)) {
    return {
      impact: "azure-write",
      risk: "high",
      summary: "This can change Azure resources, storage, or ElasticBLAST job state.",
      checks: ["Run az account show -o table.", "Confirm subscription and resource group.", "Estimate cost or resource impact first."],
      rollback: "Capture resource names and task ids so the change can be stopped or cleaned up.",
      saferCommand: normalized.startsWith("azcopy") ? "azcopy --help" : "az account show -o table",
      confidence: "high",
    };
  }

  if (matchesAny(normalized, AZURE_READ_PATTERNS)) {
    return {
      impact: "azure-read",
      risk: "medium",
      summary: "This reads Azure state using the terminal sidecar's interactive Azure login.",
      checks: ["Confirm az login is active.", "Check the selected subscription."],
      rollback: null,
      saferCommand: null,
      confidence: "high",
    };
  }

  if (matchesAny(normalized, LOCAL_WRITE_PATTERNS)) {
    return {
      impact: "local-write",
      risk: "medium",
      summary: "This writes files inside the terminal home or current working directory.",
      checks: ["Run pwd first.", "Check available space with df -h.", "Confirm output filenames do not overwrite important files."],
      rollback: "Keep source files and remove generated outputs only after verifying names.",
      saferCommand: null,
      confidence: "high",
    };
  }

  if (matchesAny(normalized, NOOP_PATTERNS)) {
    return {
      impact: "local-read",
      risk: "low",
      summary: "This is a harmless shell builtin or inspection command.",
      checks: ["No resource or file is changed by this command."],
      rollback: null,
      saferCommand: null,
      confidence: "high",
    };
  }

  if (matchesAny(normalized, LOCAL_READ_PATTERNS)) {
    return {
      impact: "local-read",
      risk: "low",
      summary: "This is a local read or inspection command.",
      checks: ["Confirm the file or folder exists."],
      rollback: null,
      saferCommand: null,
      confidence: "high",
    };
  }

  return {
    impact: "unknown",
    risk: "medium",
    summary: "This command is not recognised by the cockpit classifier yet.",
    checks: ["Inspect the command help first.", "Run a read-only version when possible."],
    rollback: null,
    saferCommand: null,
    confidence: "low",
  };
}

export function normaliseCommandForTerminalInsert(command: string): string {
  return command.replace(/[\r\n]+/g, " ").replace(/[\u0000-\u001f\u007f]/g, "").trim();
}

export interface PasteAnalysis {
  /** Number of non-empty command lines the paste would feed to the shell. */
  lineCount: number;
  /** True when the paste would run more than one command line (newline-joined). */
  isMultiline: boolean;
  /** Total characters in the normalised payload. */
  length: number;
}

/**
 * Inspect clipboard text before it reaches the PTY. A multi-line paste in a
 * shell runs each line as a separate command the instant it arrives, so the
 * terminal asks for confirmation in that case. A single line with one trailing
 * newline is treated as a normal "paste and run" and is NOT flagged.
 */
export function analysePastePayload(text: string): PasteAnalysis {
  const normalised = text.replace(/\r\n?/g, "\n");
  const segments = normalised.split("\n");
  // A single trailing newline (one command + Enter) is not "multi-line".
  if (segments.length > 0 && segments[segments.length - 1] === "") {
    segments.pop();
  }
  const nonEmpty = segments.filter((segment) => segment.trim().length > 0);
  return {
    lineCount: nonEmpty.length,
    isMultiline: segments.length > 1,
    length: text.length,
  };
}

export const COCKPIT_WORKFLOWS: CockpitWorkflow[] = [
  { id: "login", label: "Azure login", command: "az login --use-device-code", intent: "Start browser-based Azure authentication.", impact: "azure-read" },
  { id: "context", label: "Azure context", command: "az account show -o table", intent: "Confirm tenant, subscription, and account before cloud work.", impact: "azure-read" },
  { id: "tools", label: "Tool versions", command: "elb-tool-versions", intent: "Print installed terminal tool versions.", impact: "local-read" },
  { id: "files", label: "File overview", command: "pwd && ls -lh && tree -L 2", intent: "Understand the current workspace before running analysis.", impact: "local-read" },
  { id: "fasta-stats", label: "FASTA stats", command: "seqkit stats *.fa", intent: "Summarise sequence files before BLAST.", impact: "local-read" },
  { id: "make-db", label: "Build local DB", command: "makeblastdb -in refs.fa -dbtype nucl -out refs", intent: "Create a local nucleotide BLAST database.", impact: "local-write" },
  { id: "local-blast", label: "Local BLAST", command: "blastn -query query.fa -db refs -outfmt 6 -out hits.tsv", intent: "Run a local tabular BLAST search.", impact: "local-write" },
  { id: "k8s", label: "Kubernetes check", command: "kubectl get pods -A", intent: "Inspect Kubernetes state without mutation.", impact: "kubernetes" },
];

export const COCKPIT_CHAPTERS: CockpitChapter[] = [
  { id: "auth", label: "Authenticate", status: "active", detail: "Confirm Azure login and subscription before cloud work." },
  { id: "stage", label: "Stage data", status: "next", detail: "Inspect query/reference files and disk space." },
  { id: "prepare", label: "Prepare analysis", status: "next", detail: "Build local DBs or verify remote BLAST databases." },
  { id: "run", label: "Run search", status: "next", detail: "Launch local BLAST or ElasticBLAST only after preview." },
  { id: "review", label: "Review outputs", status: "next", detail: "Inspect generated files and preserve provenance." },
];

// Live signals the cockpit can observe to advance the session chapters. Each
// flag maps 1:1 to a chapter in COCKPIT_CHAPTERS order. They are derived from
// real state (Azure CLI sign-in, the commands the user actually inserted, and
// whether BLAST triage produced evidence) rather than being hard-coded.
export interface SessionChapterSignals {
  azureSignedIn: boolean;
  stagedData: boolean;
  preparedAnalysis: boolean;
  ranSearch: boolean;
  reviewedOutputs: boolean;
}

export const EMPTY_SESSION_CHAPTER_SIGNALS: SessionChapterSignals = {
  azureSignedIn: false,
  stagedData: false,
  preparedAnalysis: false,
  ranSearch: false,
  reviewedOutputs: false,
};

/**
 * Turn the static chapter template into a live ladder. A chapter whose signal
 * is satisfied is "ready" (done); the first unsatisfied chapter is "active";
 * everything after it is "next". This replaces the previous hard-coded array
 * that always reported "Authenticate = active" regardless of real progress.
 */
export function deriveSessionChapters(signals: SessionChapterSignals): CockpitChapter[] {
  const done: boolean[] = [
    signals.azureSignedIn,
    signals.stagedData,
    signals.preparedAnalysis,
    signals.ranSearch,
    signals.reviewedOutputs,
  ];
  const activeIndex = done.findIndex((value) => !value);
  return COCKPIT_CHAPTERS.map((chapter, index) => {
    let status: CockpitChapter["status"];
    if (done[index]) {
      status = "ready";
    } else if (index === activeIndex) {
      status = "active";
    } else {
      status = "next";
    }
    return { ...chapter, status };
  });
}

/**
 * Fold the commands a user has actually executed (plus Azure sign-in and triage
 * state) into chapter signals. `executedCommands` are the command lines the
 * terminal genuinely ran — both lines typed directly and cockpit inserts that
 * were run — so the ladder reflects real activity rather than typed-but-unrun
 * previews. Each command is read with the same classifier the preview uses.
 */
export function deriveChapterSignalsFromActivity(input: {
  azureSignedIn: boolean;
  executedCommands: string[];
  hasTriageEvidence: boolean;
}): SessionChapterSignals {
  const STAGE = /\b(ls|pwd|tree|seqkit\s+stats|cat|head|tail|df|du|file)\b/;
  const PREPARE = /\b(makeblastdb|elb-cfg|update_blastdb|az\s+.*\b(show|list)\b.*\b(storage|blob|container)\b)/;
  const SEARCH = /\b(blastn|blastp|blastx|tblastn|tblastx|elastic-blast\s+submit|elb\s+submit)\b/;
  // Reviewing outputs means actually inspecting result files, not merely naming
  // a results path in a submit command. Require a concrete output-inspection
  // verb so `elastic-blast submit --results run-1` does not falsely complete it.
  const REVIEW =
    /(\b(cat|head|tail|less|bat|column)\b[^\n|;]*\.tsv\b|\bhits?\.tsv\b|\boutfmt\s*6\b|\bazcopy\s+(copy|list)\b)/;
  const joined = input.executedCommands.join("\n");
  return {
    azureSignedIn: input.azureSignedIn || /\baz\s+login\b/.test(joined),
    stagedData: STAGE.test(joined),
    preparedAnalysis: PREPARE.test(joined),
    ranSearch: SEARCH.test(joined),
    reviewedOutputs: input.hasTriageEvidence || REVIEW.test(joined),
  };
}

// Tier is assigned from verifiable evidence, not aspiration:
//   shipped  → a Cockpit panel / terminal guard / elb-cfg helper renders or
//              enforces this today (see TerminalCockpit.tsx and the terminal
//              command_guard.sh / banner.sh sidecar scripts).
//   roadmap  → designed but not yet wired to a user-reachable surface.
export const INNOVATION_CAPABILITIES: InnovationCapability[] = [
  // --- shipped: backed by a real Cockpit panel, terminal guard, or CLI helper ---
  { id: "intent", label: "Command Intent Preview", tier: "shipped", icon: SearchCheck },
  { id: "safe-run", label: "Safe Run Guard", tier: "shipped", icon: ShieldCheck },
  { id: "undo", label: "Rollback Hints", tier: "shipped", icon: Undo2 },
  { id: "workflow", label: "Research Workflow Mode", tier: "shipped", icon: Compass },
  { id: "palette", label: "Shell Command Palette", tier: "shipped", icon: TerminalSquare },
  { id: "azure-context", label: "Azure Context Guard", tier: "shipped", icon: CloudCog },
  { id: "confidence", label: "Command Confidence", tier: "shipped", icon: Gauge },
  { id: "chapters", label: "Session Chapters", tier: "shipped", icon: BookMarked },
  { id: "forms", label: "Parameter Forms", tier: "shipped", icon: Braces },
  { id: "paste", label: "Smart Paste Protection", tier: "shipped", icon: ShieldAlert },
  { id: "health", label: "Terminal Health Bar", tier: "shipped", icon: Activity },
  { id: "impact", label: "Impact Replay", tier: "shipped", icon: GitCompareArrows },
  { id: "checks", label: "Pre-flight Checklist", tier: "shipped", icon: CheckCircle2 },
  { id: "diagnostic-presets", label: "Diagnostic Workflow Presets", tier: "shipped", icon: Compass },
  { id: "sample-context", label: "Sample Context Panel", tier: "shipped", icon: ClipboardCheck },
  { id: "blast-triage", label: "BLAST Result Triage", tier: "shipped", icon: FileSearch },
  { id: "controls", label: "Control Sample Awareness", tier: "shipped", icon: CheckCircle2 },
  { id: "evidence-summary", label: "Evidence Summary Boundary", tier: "shipped", icon: FileStack },
  { id: "cold-review", label: "Cold Review Checklist", tier: "shipped", icon: SearchCheck },
  { id: "maturity-ladder", label: "1-10 Maturity Ladder", tier: "shipped", icon: Gauge },
  { id: "input-qc-gates", label: "Input QC Gates", tier: "shipped", icon: CheckCircle2 },
  { id: "db-provenance-gates", label: "DB Provenance Gates", tier: "shipped", icon: Database },
  { id: "db", label: "BLAST DB Awareness", tier: "shipped", icon: Database },
  // --- roadmap: designed, not yet wired to a user-reachable surface ---
  { id: "file-rail", label: "Live File Rail", tier: "roadmap", icon: FolderTree },
  { id: "artifacts", label: "Artifact Detection", tier: "roadmap", icon: PackageCheck },
  { id: "bio-preview", label: "Bio File Preview", tier: "roadmap", icon: FileSearch },
  { id: "nl-builder", label: "Natural Command Builder", tier: "roadmap", icon: Wand2 },
  { id: "explain", label: "Explain Output", tier: "roadmap", icon: BrainCircuit },
  { id: "autopsy", label: "Failure Autopsy", tier: "roadmap", icon: AlertTriangle },
  { id: "cost", label: "Cost Awareness", tier: "roadmap", icon: Coins },
  { id: "runbook", label: "Runbook Export", tier: "roadmap", icon: FileCode2 },
  { id: "checkpoint", label: "Checkpoint Re-run", tier: "roadmap", icon: TimerReset },
  { id: "semantic-history", label: "Semantic History", tier: "roadmap", icon: History },
  { id: "snippets", label: "Pinned Snippets", tier: "roadmap", icon: ClipboardCheck },
  { id: "secrets", label: "Secret Leak Guard", tier: "roadmap", icon: LockKeyhole },
  { id: "handoff", label: "Collaborative Handoff", tier: "roadmap", icon: SplitSquareHorizontal },
  { id: "diff", label: "Generated File Diff", tier: "roadmap", icon: FileDiff },
  { id: "prompt", label: "Result-aware Prompt", tier: "roadmap", icon: Code2 },
  { id: "sandbox", label: "Sandbox Levels", tier: "roadmap", icon: ShieldCheck },
  { id: "ai-boundary", label: "AI Pair Operator", tier: "roadmap", icon: Sparkles },
  { id: "long-run", label: "Long-running Companion", tier: "roadmap", icon: PlayCircle },
  { id: "autocomplete", label: "Domain Autocomplete", tier: "roadmap", icon: ListChecks },
  { id: "provenance", label: "Data Provenance Trail", tier: "roadmap", icon: FileStack },
  { id: "dashboard-sync", label: "Dashboard Sync", tier: "roadmap", icon: Network },
  { id: "permissions", label: "Readable Permissions", tier: "roadmap", icon: KeyRound },
  { id: "recover", label: "Recovery Checklist", tier: "roadmap", icon: RotateCcw },
  { id: "panic", label: "Beginner Panic Button", tier: "roadmap", icon: LifeBuoy },
];
