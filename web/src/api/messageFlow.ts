/**
 * messageFlow — typed client for the dashboard Message Flow card.
 *
 * Surfaces the optional Service Bus integration as a three-lane flow:
 * Producers (active-job submitters) -> Broker (active BLAST jobs, sized by
 * query length) -> Consumers (AKS clusters). The backend returns
 * `{ enabled: false }` whenever the integration is off, so the card hides
 * itself and the default experience is unchanged.
 *
 * The "view JSON" affordance in the modal reuses the existing monitor job
 * detail endpoint (`/monitor/jobs/{id}`) to show the real JobState payload.
 */
import { api } from "@/api/client";
import type { ServiceBusCounts } from "@/api/settings";

export type SubmissionSource = "dashboard" | "external_api" | "servicebus";

/**
 * Where a broker job sits in its lifecycle for the constellation:
 * - `active` — currently in flight (queued/pending/running/reducing).
 * - `settling` — recently terminal (completed/failed/cancelled) and fading out;
 *   the backend keeps it for a short window so a finished/failed job does not
 *   vanish the instant it leaves the active set.
 */
export type JobLifecycle = "active" | "settling";

export interface MessageFlowProducer {
  alias: string;
  job_count: number;
  sources: SubmissionSource[];
}

export interface MessageFlowBox {
  job_id: string;
  program: string | null;
  db: string | null;
  status: string;
  phase: string | null;
  query_label: string | null;
  /** Sequence-letter count used to size the box; null when unknown. */
  query_size: number | null;
  alias: string;
  submission_source: SubmissionSource;
  cluster_name: string;
  created_at: string | null;
  /** Terminal-transition time, used to time the settling fade. */
  updated_at?: string | null;
  /** `active` while in flight, `settling` while a terminal job fades out. */
  lifecycle?: JobLifecycle;
  /** Short error identifier for a failed job (never a full error body). */
  error_code?: string | null;
}

export interface MessageFlowCluster {
  cluster_name: string;
  resource_group: string;
  subscription_id: string;
  running: number;
  queued: number;
  /** Recently-terminal jobs targeting this cluster (fading out); never counted
   *  in running/queued/total. */
  settling?: number;
  total: number;
}

export interface MessageFlowSnapshot {
  enabled: boolean;
  /** "own" = caller's active jobs only; "shared" = every submitter (dev flag). */
  scope?: "own" | "shared";
  namespace_fqdn?: string;
  request_queue?: string;
  completion_topic?: string;
  sb_counts?: ServiceBusCounts;
  active_total?: number;
  /** Recently-terminal jobs still drawn (fading out), not part of active_total. */
  settling_total?: number;
  /** Number of broker boxes actually returned (≤ active_total + settling_total). */
  active_shown?: number;
  /** True when more visible jobs exist than the broker box cap. */
  broker_truncated?: boolean;
  /** True when the table read window was hit (counts are a floor, not total). */
  read_truncated?: boolean;
  producers?: MessageFlowProducer[];
  broker?: MessageFlowBox[];
  consumers?: { clusters: MessageFlowCluster[] };
}

/** Raw JobState detail returned by the monitor job endpoint (for the JSON view). */
export interface MonitorJobDetail {
  state: Record<string, unknown> | null;
  history?: unknown[];
}

export const messageFlowApi = {
  get: () => api.get<MessageFlowSnapshot>("/monitor/message-flow"),
  getJobDetail: (jobId: string) =>
    api.get<MonitorJobDetail>(`/monitor/jobs/${encodeURIComponent(jobId)}`),
};
