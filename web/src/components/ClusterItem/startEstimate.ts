/**
 * startEstimate — shared timing math for the AKS "Starting…" state.
 *
 * Owns the fallback phase constants, the warmup window model, the live
 * elapsed ticker, and the `/monitor/aks/start-stats` query. Both the
 * always-visible cluster status line and the expanded `StartEstimatePanel`
 * consume `useStartProgress` so the elapsed / API-ready / warm-cache
 * estimates stay consistent across surfaces. Kept side-effect free apart
 * from the 1 s ticker, which is gated on an active start.
 */

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { monitoringApi } from "@/api/endpoints";

// Fallback estimates used until the backend has recorded at least one real
// observation for a phase (mirrors api/services/cluster_timings.DEFAULT_SECONDS).
export const OBSERVED_AKS_START_SECONDS = 235;
export const OBSERVED_OPENAPI_DEPLOY_SECONDS = 31;
const WARMUP_FIRST_DB_LOW_SECONDS = 10 * 60;
const WARMUP_FIRST_DB_HIGH_SECONDS = 25 * 60;
const WARMUP_EXTRA_DB_LOW_SECONDS = 6 * 60;
const WARMUP_EXTRA_DB_HIGH_SECONDS = 15 * 60;

/** Human "2 min" / "1 hr 5 min" — coarse, used for estimate copy. */
export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))} sec`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} hr ${rest} min` : `${hours} hr`;
}

/** Compact "1m 20s" / "45s" — used for the live elapsed/remaining clock. */
export function formatClock(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) {
    const rest = total % 60;
    return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
  }
  const hours = Math.floor(minutes / 60);
  const restMin = minutes % 60;
  return restMin ? `${hours}h ${restMin}m` : `${hours}h`;
}

export function warmupRangeSeconds(dbCount: number): [number, number] {
  if (dbCount <= 0) return [0, 0];
  return [
    WARMUP_FIRST_DB_LOW_SECONDS + (dbCount - 1) * WARMUP_EXTRA_DB_LOW_SECONDS,
    WARMUP_FIRST_DB_HIGH_SECONDS + (dbCount - 1) * WARMUP_EXTRA_DB_HIGH_SECONDS,
  ];
}

export interface StartProgress {
  /** Seconds since the start action was issued. */
  elapsedSeconds: number;
  /** Median observed (or default) AKS start duration. */
  aksStartSeconds: number;
  /** Median observed (or default) OpenAPI deploy duration. */
  openapiSeconds: number;
  /** aksStart + openapi — when the API is expected to answer. */
  apiReadySeconds: number;
  /** Estimated seconds left until API readiness (clamped ≥ 0). */
  apiRemainingSeconds: number;
  warmupLowSeconds: number;
  warmupHighSeconds: number;
  totalLowSeconds: number;
  totalHighSeconds: number;
  /** Estimated seconds left until warm cache is ready (high bound, ≥ 0). */
  warmRemainingSeconds: number;
  /** True once the start-stats query reports measured (not default) data. */
  isMeasured: boolean;
  aksStartSamples: number;
}

/**
 * Live timing model for a starting cluster. The 1 s ticker only runs while
 * `startedAt` is set (i.e. the cluster is actually starting), so an idle row
 * does not re-render every second.
 */
export function useStartProgress({
  startedAt,
  autoWarmupDbCount,
}: {
  startedAt: number | null;
  autoWarmupDbCount: number;
}): StartProgress {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (startedAt == null) return;
    const tick = () => {
      if (!document.hidden) setNow(Date.now());
    };
    tick();
    const timer = window.setInterval(tick, 1_000);
    document.addEventListener("visibilitychange", tick);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [startedAt]);

  const statsQuery = useQuery({
    queryKey: ["aks-start-stats"],
    queryFn: () => monitoringApi.aksStartStats(),
    staleTime: 5 * 60_000,
    retry: 0,
  });
  const stats = statsQuery.data;
  const aksStartPhase = stats?.phases?.aks_start;
  const openapiPhase = stats?.phases?.openapi_deploy;
  const aksStartSeconds = aksStartPhase?.seconds ?? OBSERVED_AKS_START_SECONDS;
  const openapiSeconds = openapiPhase?.seconds ?? OBSERVED_OPENAPI_DEPLOY_SECONDS;
  const aksStartSamples = aksStartPhase?.samples ?? 0;
  const isMeasured = (aksStartPhase?.source ?? "default") === "measured";

  const elapsedSeconds = startedAt
    ? Math.max(0, Math.floor((now - startedAt) / 1_000))
    : 0;
  const apiReadySeconds = Math.round(aksStartSeconds + openapiSeconds);
  const [warmupLowSeconds, warmupHighSeconds] = warmupRangeSeconds(autoWarmupDbCount);
  const totalLowSeconds = apiReadySeconds + warmupLowSeconds;
  const totalHighSeconds = apiReadySeconds + warmupHighSeconds;

  return {
    elapsedSeconds,
    aksStartSeconds,
    openapiSeconds,
    apiReadySeconds,
    apiRemainingSeconds: Math.max(0, apiReadySeconds - elapsedSeconds),
    warmupLowSeconds,
    warmupHighSeconds,
    totalLowSeconds,
    totalHighSeconds,
    warmRemainingSeconds:
      autoWarmupDbCount > 0 ? Math.max(0, totalHighSeconds - elapsedSeconds) : 0,
    isMeasured,
    aksStartSamples,
  };
}

/**
 * Compact one-line progress for the always-visible cluster status line,
 * e.g. `Starting · 1m 20s elapsed · API ~2m · warm ~12m`.
 */
export function startingStatusLine(
  progress: StartProgress,
  autoWarmupDbCount: number,
): string {
  const parts = [`Starting · ${formatClock(progress.elapsedSeconds)} elapsed`];
  parts.push(
    progress.apiRemainingSeconds > 0
      ? `API ~${formatClock(progress.apiRemainingSeconds)}`
      : "API readying",
  );
  if (autoWarmupDbCount > 0) {
    parts.push(
      progress.warmRemainingSeconds > 0
        ? `warm ~${formatClock(progress.warmRemainingSeconds)}`
        : "warm readying",
    );
  }
  return parts.join(" · ");
}
