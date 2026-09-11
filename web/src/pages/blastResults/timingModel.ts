/**
 * Pure view-model helpers for stable BLAST queue, processing, and result timing.
 */
import type { BlastJobSummary } from "@/api/endpoints";

export function finiteSeconds(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? Math.floor(value)
    : null;
}

export function formatTimingSeconds(value: unknown): string {
  const seconds = finiteSeconds(value);
  if (seconds === null) return "—";
  const days = Math.floor(seconds / 86_400);
  const hours = Math.floor((seconds % 86_400) / 3_600);
  const minutes = Math.floor((seconds % 3_600) / 60);
  const remainder = seconds % 60;
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${remainder}s`;
  return `${remainder}s`;
}

export function stableProcessingSeconds(job: BlastJobSummary): number | null {
  return finiteSeconds(job.timing?.processing_seconds ?? job.run_seconds);
}

export function stableTimeToResultSeconds(job: BlastJobSummary): number | null {
  return finiteSeconds(job.timing?.time_to_result_seconds ?? job.elapsed_seconds);
}

export function stableQueueSeconds(job: BlastJobSummary): number | null {
  return finiteSeconds(job.timing?.total_queue_seconds ?? job.queue_wait_seconds);
}
