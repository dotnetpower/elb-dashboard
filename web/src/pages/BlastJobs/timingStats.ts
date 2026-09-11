import type { BlastJobSummary } from "@/api/endpoints";

import { finiteSeconds } from "../blastResults/timingModel";

export interface RecentTimingStats {
  totalJobs: number;
  completeTimeToResultSamples: number;
  completeQueueSamples: number;
  processingSamples: number;
  pressureSamples: number;
  timeToResultP50: number | null;
  timeToResultP95: number | null;
  queueP50: number | null;
  queueP95: number | null;
  processingP50: number | null;
  processingP95: number | null;
  queuePressure: "unknown" | "normal" | "high";
}

const MIN_PRESSURE_SAMPLES = 5;

function percentile(values: number[], fraction: number): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((left, right) => left - right);
  const index = Math.max(0, Math.ceil(fraction * sorted.length) - 1);
  return sorted[Math.min(index, sorted.length - 1)];
}

export function recentTimingStats(jobs: BlastJobSummary[]): RecentTimingStats {
  const timeToResult: number[] = [];
  const queue: number[] = [];
  const processing: number[] = [];
  const pressureQueue: number[] = [];
  const pressureProcessing: number[] = [];

  for (const job of jobs) {
    const timing = job.timing;
    if (!timing) continue;
    const resultSeconds = finiteSeconds(timing.time_to_result_seconds);
    if (resultSeconds !== null && timing.queue_complete) timeToResult.push(resultSeconds);
    const queueSeconds = finiteSeconds(timing.total_queue_seconds);
    if (queueSeconds !== null && timing.queue_complete) queue.push(queueSeconds);
    const processingSeconds = finiteSeconds(timing.processing_seconds);
    if (processingSeconds !== null) processing.push(processingSeconds);
    if (
      timing.breakdown_complete &&
      queueSeconds !== null &&
      processingSeconds !== null
    ) {
      pressureQueue.push(queueSeconds);
      pressureProcessing.push(processingSeconds);
    }
  }

  const queueP50 = percentile(queue, 0.5);
  const queueP95 = percentile(queue, 0.95);
  const processingP50 = percentile(processing, 0.5);
  const processingP95 = percentile(processing, 0.95);
  const pressureQueueP95 = percentile(pressureQueue, 0.95);
  const pressureProcessingP50 = percentile(pressureProcessing, 0.5);
  const queuePressure =
    pressureQueue.length < MIN_PRESSURE_SAMPLES ||
    pressureQueueP95 === null ||
    pressureProcessingP50 === null
      ? "unknown"
      : pressureQueueP95 > pressureProcessingP50
        ? "high"
        : "normal";

  return {
    totalJobs: jobs.length,
    completeTimeToResultSamples: timeToResult.length,
    completeQueueSamples: queue.length,
    processingSamples: processing.length,
    pressureSamples: pressureQueue.length,
    timeToResultP50: percentile(timeToResult, 0.5),
    timeToResultP95: percentile(timeToResult, 0.95),
    queueP50,
    queueP95,
    processingP50,
    processingP95,
    queuePressure,
  };
}
