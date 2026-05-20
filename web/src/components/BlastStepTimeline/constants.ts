import {
  Server,
  HardDrive,
  Upload,
  Settings,
  Dna,
  Send,
  Package,
  Trophy,
  type LucideIcon,
} from "lucide-react";

export interface PhaseStep {
  key: string;
  label: string;
  desc: string;
  icon: LucideIcon;
}

// Phase steps matching orchestrator order exactly.
export const PHASE_STEPS: PhaseStep[] = [
  {
    key: "checking_vm",
    label: "Prepare VM",
    desc: "Start remote terminal",
    icon: Server,
  },
  {
    key: "enabling_storage",
    label: "Open Storage",
    desc: "Enable public access",
    icon: HardDrive,
  },
  {
    key: "uploading",
    label: "Upload Query",
    desc: "Send sequence to blob",
    icon: Upload,
  },
  { key: "configuring", label: "Configure", desc: "Generate INI config", icon: Settings },
  { key: "warming_up", label: "Warmup", desc: "Prepare DB shards on SSD", icon: Dna },
  { key: "submitting", label: "Submit Job", desc: "Send to AKS cluster", icon: Send },
  { key: "running", label: "BLAST Run", desc: "Sequence alignment", icon: Dna },
  {
    key: "exporting_results",
    label: "Export",
    desc: "Copy results to blob",
    icon: Package,
  },
  { key: "completed", label: "Complete", desc: "All done!", icon: Trophy },
];

export const FAILURE_PHASES = new Set([
  "failed",
  "error",
  "submit_failed",
  "split_submit_invalid",
  "split_results_merge_invalid",
  "warmup_failed",
]);

export type StepState = "done" | "active" | "pending" | "error" | "skipped";

export const PHASE_TO_STEP: Record<string, string> = {
  submit_failed: "submitting",
  reading_split_query: "uploading",
  splitting_queries: "configuring",
  split_children_submitted: "submitting",
  split_children_aggregating: "running",
  split_children_merge_ready: "exporting_results",
  split_results_waiting_for_artifacts: "exporting_results",
  split_results_merging: "exporting_results",
  results_pending: "exporting_results",
  split_submit_invalid: "submitting",
  split_results_merge_invalid: "exporting_results",
  warmup_failed: "warming_up",
};

export const PHASE_MESSAGES: Record<string, string> = {
  checking_vm: "Verifying Terminal sidecar is reachable...",
  enabling_storage: "Enabling storage public access for data transfer...",
  uploading: "Uploading query sequence to Azure Blob Storage...",
  configuring: "Generating ElasticBLAST configuration...",
  warming_up: "Preparing cluster with DB shards on local SSD (warmup)...",
  warmup_failed: "Cluster warmup failed.",
  submitting: "Submitting job to AKS cluster...",
  reading_split_query: "Reading the original query from Storage...",
  splitting_queries: "Splitting queries by effective search space...",
  split_children_submitted: "Submitted split child jobs to AKS...",
  split_children_aggregating: "Waiting for split child jobs to finish...",
  split_children_merge_ready: "Split child jobs are ready for result assembly...",
  split_results_waiting_for_artifacts: "Waiting for split child result artifacts...",
  split_results_merging: "Assembling split child results...",
  results_pending: "Waiting for final BLAST result files...",
  split_submit_invalid: "Split submit request is invalid.",
  split_results_merge_invalid: "Split result assembly failed validation.",
  running: "BLAST search is running on the cluster...",
  exporting_results: "Verifying result files and exporting logs from cluster...",
  completed: "Job completed successfully!",
  failed: "Job failed.",
  error: "An error occurred.",
};

// Shimmer animation for active steps.
export const SHIMMER_STYLE = `
@keyframes step-shimmer {
  0% { transform: translateX(-100%); }
  100% { transform: translateX(300%); }
}
`;
