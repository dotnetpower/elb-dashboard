import {
  Server,
  HardDrive,
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

// Phase steps matching the Container Apps + AKS orchestrator order.
export const PHASE_STEPS: PhaseStep[] = [
  {
    key: "preparing",
    label: "Prepare Run",
    desc: "Validate submit inputs",
    icon: Server,
  },
  {
    key: "warming_up",
    label: "Warmup Check",
    desc: "Confirm DB shards are warm",
    icon: Dna,
  },
  { key: "configuring", label: "Configure", desc: "Generate INI config", icon: Settings },
  { key: "staging_db", label: "Stage DB", desc: "Reuse or stage node SSD", icon: HardDrive },
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
  checking_vm: "preparing",
  enabling_storage: "preparing",
  uploading: "preparing",
  warmup_ready: "warming_up",
  waiting_for_warmup: "warming_up",
  submit_failed: "submitting",
  staging_db: "staging_db",
  reading_split_query: "preparing",
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
  preparing: "Preparing the BLAST run...",
  checking_vm: "Preparing the BLAST run...",
  enabling_storage: "Preparing the BLAST run...",
  uploading: "Preparing the BLAST run...",
  configuring: "Generating ElasticBLAST configuration...",
  warming_up: "Checking node-local DB warmup readiness...",
  warmup_failed: "Cluster warmup failed.",
  warmup_ready: "Node-local DB warmup is ready.",
  waiting_for_warmup: "Waiting for node-local DB warmup...",
  staging_db: "Reusing or staging DB shards on node-local SSD...",
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
