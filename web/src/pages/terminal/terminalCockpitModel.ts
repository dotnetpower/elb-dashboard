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

export interface CommandAnalysis {
  impact: CommandImpact;
  risk: CommandRisk;
  summary: string;
  checks: string[];
  rollback: string | null;
  saferCommand: string | null;
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

export interface InnovationCapability {
  id: string;
  label: string;
  status: "live" | "guarded" | "foundation";
  icon: LucideIcon;
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
const LOCAL_WRITE_PATTERNS = [/>/, /\b(mkdir|cp|mv|touch|nano|vim|makeblastdb|pigz|tar\s+-x|unzip)\b/];
const AZURE_READ_PATTERNS = [/az\s+.*\b(list|show|get)\b/, /azcopy\s+list\b/];
const LOCAL_READ_PATTERNS = [/\b(pwd|ls|cat|less|head|tail|file|tree|seqkit\s+stats|blastn\s+-version|which|command\s+-v)\b/];

function matchesAny(command: string, patterns: RegExp[]): boolean {
  return patterns.some((pattern) => pattern.test(command));
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
    };
  }

  if (matchesAny(normalized, DESTRUCTIVE_PATTERNS)) {
    return {
      impact: "destructive",
      risk: "high",
      summary: "This can delete or mutate resources and should not run without an explicit recovery plan.",
      checks: ["Confirm the target path or resource name.", "Snapshot important files first.", "Prefer listing targets before deletion."],
      rollback: "Rollback may require restoring from backups or recreating cloud resources.",
      saferCommand: normalized.startsWith("kubectl delete") ? normalized.replace("delete", "get") : null,
    };
  }

  if (matchesAny(normalized, KUBERNETES_PATTERNS)) {
    return {
      impact: "kubernetes",
      risk: matchesAny(normalized, [/\b(apply|scale|rollout|exec|delete)\b/]) ? "high" : "medium",
      summary: "This talks to the active Kubernetes context from the terminal sidecar.",
      checks: ["Check kubectl config current-context.", "Confirm namespace and cluster.", "Prefer read-only get/describe before mutation."],
      rollback: "Use the previous manifest, scale value, or job state if the command changes cluster state.",
      saferCommand: normalized.includes("kubectl get") ? null : "kubectl get pods -A",
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
    };
  }

  return {
    impact: "unknown",
    risk: "medium",
    summary: "This command is not recognised by the cockpit classifier yet.",
    checks: ["Inspect the command help first.", "Run a read-only version when possible."],
    rollback: null,
    saferCommand: null,
  };
}

export function normaliseCommandForTerminalInsert(command: string): string {
  return command.replace(/[\r\n]+/g, " ").replace(/[\u0000-\u001f\u007f]/g, "").trim();
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

export const INNOVATION_CAPABILITIES: InnovationCapability[] = [
  { id: "intent", label: "Command Intent Preview", status: "live", icon: SearchCheck },
  { id: "safe-run", label: "Safe Run Guard", status: "live", icon: ShieldCheck },
  { id: "undo", label: "Rollback Hints", status: "guarded", icon: Undo2 },
  { id: "workflow", label: "Research Workflow Mode", status: "live", icon: Compass },
  { id: "file-rail", label: "Live File Rail", status: "foundation", icon: FolderTree },
  { id: "artifacts", label: "Artifact Detection", status: "foundation", icon: PackageCheck },
  { id: "bio-preview", label: "Bio File Preview", status: "foundation", icon: FileSearch },
  { id: "palette", label: "Shell Command Palette", status: "live", icon: TerminalSquare },
  { id: "nl-builder", label: "Natural Command Builder", status: "guarded", icon: Wand2 },
  { id: "explain", label: "Explain Output", status: "foundation", icon: BrainCircuit },
  { id: "autopsy", label: "Failure Autopsy", status: "foundation", icon: AlertTriangle },
  { id: "cost", label: "Cost Awareness", status: "guarded", icon: Coins },
  { id: "azure-context", label: "Azure Context Guard", status: "live", icon: CloudCog },
  { id: "confidence", label: "Command Confidence", status: "live", icon: Gauge },
  { id: "chapters", label: "Session Chapters", status: "live", icon: BookMarked },
  { id: "runbook", label: "Runbook Export", status: "foundation", icon: FileCode2 },
  { id: "checkpoint", label: "Checkpoint Re-run", status: "foundation", icon: TimerReset },
  { id: "semantic-history", label: "Semantic History", status: "foundation", icon: History },
  { id: "snippets", label: "Pinned Snippets", status: "live", icon: ClipboardCheck },
  { id: "forms", label: "Parameter Forms", status: "foundation", icon: Braces },
  { id: "paste", label: "Smart Paste Protection", status: "guarded", icon: ShieldAlert },
  { id: "secrets", label: "Secret Leak Guard", status: "guarded", icon: LockKeyhole },
  { id: "handoff", label: "Collaborative Handoff", status: "foundation", icon: SplitSquareHorizontal },
  { id: "diff", label: "Generated File Diff", status: "foundation", icon: FileDiff },
  { id: "health", label: "Terminal Health Bar", status: "live", icon: Activity },
  { id: "prompt", label: "Result-aware Prompt", status: "foundation", icon: Code2 },
  { id: "sandbox", label: "Sandbox Levels", status: "guarded", icon: ShieldCheck },
  { id: "ai-boundary", label: "AI Pair Operator", status: "guarded", icon: Sparkles },
  { id: "long-run", label: "Long-running Companion", status: "foundation", icon: PlayCircle },
  { id: "autocomplete", label: "Domain Autocomplete", status: "foundation", icon: ListChecks },
  { id: "impact", label: "Impact Replay", status: "live", icon: GitCompareArrows },
  { id: "panic", label: "Beginner Panic Button", status: "live", icon: LifeBuoy },
  { id: "provenance", label: "Data Provenance Trail", status: "foundation", icon: FileStack },
  { id: "dashboard-sync", label: "Dashboard Sync", status: "foundation", icon: Network },
  { id: "permissions", label: "Readable Permissions", status: "guarded", icon: KeyRound },
  { id: "recover", label: "Recovery Checklist", status: "live", icon: RotateCcw },
  { id: "db", label: "BLAST DB Awareness", status: "foundation", icon: Database },
  { id: "checks", label: "Pre-flight Checklist", status: "live", icon: CheckCircle2 },
  { id: "diagnostic-presets", label: "Diagnostic Workflow Presets", status: "live", icon: Compass },
  { id: "sample-context", label: "Sample Context Panel", status: "live", icon: ClipboardCheck },
  { id: "blast-triage", label: "BLAST Result Triage", status: "live", icon: FileSearch },
  { id: "controls", label: "Control Sample Awareness", status: "live", icon: CheckCircle2 },
  { id: "evidence-summary", label: "Evidence Summary Boundary", status: "live", icon: FileStack },
  { id: "cold-review", label: "Cold Review Checklist", status: "live", icon: SearchCheck },
  { id: "maturity-ladder", label: "1-10 Maturity Ladder", status: "live", icon: Gauge },
  { id: "input-qc-gates", label: "Input QC Gates", status: "live", icon: CheckCircle2 },
  { id: "db-provenance-gates", label: "DB Provenance Gates", status: "live", icon: Database },
];
