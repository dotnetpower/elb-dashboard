import type { BlastJobSummary } from "@/api/endpoints";

/**
 * Pure submit-volume aggregations for the ClusterBento hero cell.
 *
 * Extracted from `ClusterBento.tsx` (issue #24 SRP split): these are
 * side-effect-free reductions over the cluster's BLAST job list, so they
 * live in their own module and carry unit tests. No React, no I/O.
 */

/**
 * Build a per-minute "submits started" timeline for the last `windowMin`
 * minutes. Used by the hero sparkline so the line shown directly under
 * "Submit pipeline · 15m" is the actual submit volume — previously the
 * sparkline displayed `/api/blast/*` request RPM, which made the bento
 * lie when the cluster had zero submits but normal browser polling.
 */
export function submitTimeline(jobs: BlastJobSummary[], windowMin: number): number[] {
  const buckets = new Array(windowMin).fill(0) as number[];
  if (jobs.length === 0) return buckets;
  const now = Date.now();
  const start = now - windowMin * 60 * 1000;
  for (const j of jobs) {
    const ts = j.created_at ? Date.parse(j.created_at) : NaN;
    if (!Number.isFinite(ts) || ts < start || ts > now) continue;
    const idx = Math.min(windowMin - 1, Math.max(0, Math.floor((ts - start) / 60_000)));
    buckets[idx] += 1;
  }
  return buckets;
}

export interface SubmitWindow {
  last15m: number;
  last1h: number;
  last24h: number;
  last24hActive: number;
  delta: number | null;
  avgRuntimeSec: number | null;
}

export function submitWindow(jobs: BlastJobSummary[]): SubmitWindow {
  const now = Date.now();
  const w15 = now - 15 * 60 * 1000;
  const w1h = now - 60 * 60 * 1000;
  const w24h = now - 24 * 60 * 60 * 1000;
  const w15Prev = now - 30 * 60 * 1000;

  let last15m = 0;
  let last1h = 0;
  let last24h = 0;
  let last24hActive = 0;
  let prev15m = 0;
  let runtimeSum = 0;
  let runtimeCount = 0;

  for (const j of jobs) {
    const ts = j.created_at ? Date.parse(j.created_at) : NaN;
    if (!Number.isFinite(ts)) continue;
    if (ts >= w15) last15m += 1;
    if (ts >= w1h) last1h += 1;
    if (ts >= w24h) {
      last24h += 1;
      const isActive =
        j.status !== "completed" && j.status !== "failed" && j.status !== "cancelled";
      if (isActive) last24hActive += 1;
      const upd = j.updated_at ? Date.parse(j.updated_at) : ts;
      if (!isActive && Number.isFinite(upd) && upd > ts) {
        runtimeSum += (upd - ts) / 1000;
        runtimeCount += 1;
      }
    }
    if (ts >= w15Prev && ts < w15) prev15m += 1;
  }
  const delta = prev15m === 0 ? (last15m === 0 ? 0 : 1) : (last15m - prev15m) / prev15m;
  return {
    last15m,
    last1h,
    last24h,
    last24hActive,
    delta: jobs.length === 0 ? null : delta,
    avgRuntimeSec: runtimeCount === 0 ? null : runtimeSum / runtimeCount,
  };
}
