import { useEffect, useRef, useState } from "react";

import { PHASE_STEPS, PHASE_TO_STEP, type StepState } from "./constants";

function formatDuration(ms: number): string {
  if (ms > 0 && ms < 1000) return "<1s";
  const s = Math.round(ms / 1000);
  if (s >= 3600)
    return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m ${s % 60}s`;
  return s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
}

/**
 * Track per-step durations for an in-flight orchestrator run.
 *
 * Server-side `started_at` / `completed_at` always win. Client-side phase
 * transition timestamps are the live fallback so the active step's
 * elapsed timer keeps ticking while we're waiting for a fresh server
 * snapshot. A 1 s tick keeps the active step's display fresh.
 */
export function useStepDurations(args: {
  phase: string;
  stepsData: Record<string, Record<string, unknown>>;
}) {
  const { phase, stepsData } = args;
  const [, setTick] = useState(0);
  const phaseTimestamps = useRef<Record<string, number>>({});
  const phaseDurations = useRef<Record<string, number>>({});

  // Track phase transitions to calculate per-step durations.
  useEffect(() => {
    if (!phase) return;
    const now = Date.now();
    const ts = phaseTimestamps.current;

    const phaseKey = PHASE_TO_STEP[phase] ?? phase;
    const stepIdx = PHASE_STEPS.findIndex((s) => s.key === phaseKey);
    if (stepIdx >= 0 && !ts[phaseKey]) {
      ts[phaseKey] = now;
      // Mark previous step as completed with duration.
      if (stepIdx > 0) {
        const prevKey = PHASE_STEPS[stepIdx - 1].key;
        if (ts[prevKey] && !phaseDurations.current[prevKey]) {
          phaseDurations.current[prevKey] = now - ts[prevKey];
        }
      }
    }
    // If completed/failed, finalize all durations.
    if (phase === "completed" || phase === "failed") {
      for (let i = 0; i < PHASE_STEPS.length; i++) {
        const key = PHASE_STEPS[i].key;
        if (ts[key] && !phaseDurations.current[key]) {
          const nextKey = PHASE_STEPS[i + 1]?.key;
          const endTime = nextKey && ts[nextKey] ? ts[nextKey] : now;
          phaseDurations.current[key] = endTime - ts[key];
        }
      }
    }
  }, [phase]);

  // Tick every second to update active step timer.
  useEffect(() => {
    const tick = () => {
      if (!document.hidden) setTick((t) => t + 1);
    };
    const id = setInterval(tick, 1000);
    document.addEventListener("visibilitychange", tick);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", tick);
    };
  }, []);

  const getStepDuration = (key: string, state: StepState): string | null => {
    if (state === "pending" || state === "skipped") return null;

    // 1. Prefer server-side timestamps (available for completed jobs).
    const sd = stepsData[key] as Record<string, unknown> | undefined;
    if (sd?.started_at && sd?.completed_at) {
      const ms =
        new Date(sd.completed_at as string).getTime() -
        new Date(sd.started_at as string).getTime();
      if (ms >= 0) return formatDuration(ms);
    }
    const serverDurationMs = numberValue(sd?.duration_ms);
    const durationSource = stringValue(sd?.duration_source);
    if (
      serverDurationMs !== null &&
      ["k8s_runtime", "timestamps", "server_checkpoint", "result_artifact_verification"].includes(
        durationSource,
      )
    ) {
      return formatDuration(serverDurationMs);
    }
    // Server-side started_at but no completed_at → live elapsed from
    // server start.
    if (sd?.started_at && state === "active") {
      const ms = Date.now() - new Date(sd.started_at as string).getTime();
      return formatDuration(Math.max(0, ms));
    }

    // 2. Fall back to client-side tracking (live sessions).
    const dur = phaseDurations.current[key];
    if (dur) return formatDuration(dur);

    // Active step — show live elapsed from client timestamp.
    if (state === "active") {
      const start = phaseTimestamps.current[key];
      if (start) return formatDuration(Date.now() - start);
    }
    if (state === "done") return "not measured";
    return null;
  };

  return { getStepDuration };
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}
