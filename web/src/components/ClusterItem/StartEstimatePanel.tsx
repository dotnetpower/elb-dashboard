import { Clock3, DatabaseZap, Lightbulb, Network } from "lucide-react";
import { useMemo } from "react";

import { formatDuration, useStartProgress, type StartProgress } from "./startEstimate";

export function StartEstimatePanel({
  clusterName,
  autoWarmupDbCount,
  startedAt,
  progress: progressProp,
}: {
  clusterName: string;
  autoWarmupDbCount: number;
  startedAt: number | null;
  /** Pre-computed timings from the parent's `useStartProgress`. When omitted
   *  the panel derives its own (kept for standalone use / tests). */
  progress?: StartProgress;
}) {
  const ownProgress = useStartProgress({
    startedAt: progressProp ? null : startedAt,
    autoWarmupDbCount,
  });
  const progress = progressProp ?? ownProgress;

  const {
    elapsedSeconds,
    aksStartSeconds,
    openapiSeconds,
    apiReadySeconds,
    apiRemainingSeconds: apiRemaining,
    totalLowSeconds: totalLow,
    totalHighSeconds: totalHigh,
    isMeasured,
    aksStartSamples,
  } = progress;

  const tipIndex = Math.floor(elapsedSeconds / 12) % 4;

  const tips = useMemo(
    () => [
      {
        icon: Clock3,
        title: "Startup estimate",
        body: `${
          isMeasured
            ? `Median of the last ${aksStartSamples} observed AKS start${
                aksStartSamples === 1 ? "" : "s"
              } is ${formatDuration(aksStartSeconds)}`
            : `Typical AKS start is about ${formatDuration(aksStartSeconds)}`
        }. OpenAPI usually adds about ${formatDuration(
          openapiSeconds,
        )}, so API readiness is roughly ${formatDuration(apiReadySeconds)}.`,
      },
      {
        icon: DatabaseZap,
        title: "Warm cache window",
        body:
          autoWarmupDbCount > 0
            ? `${autoWarmupDbCount} auto-warm database${
                autoWarmupDbCount === 1 ? "" : "s"
              } selected. Plan on about ${formatDuration(totalLow)}-${formatDuration(
                totalHigh,
              )} end-to-end before warm cache is fully ready.`
            : "No auto-warm databases are selected. The cluster should be usable soon after AKS and OpenAPI are ready.",
      },
      {
        icon: Network,
        title: "Expected sequence",
        body: `Typical order: nodes come back first, then the OpenAPI service is checked, then warmup jobs can touch database files on each node. ${clusterName} will keep polling for actual state changes.`,
      },
      {
        icon: Lightbulb,
        title: "Tip while waiting",
        body:
          autoWarmupDbCount > 0
            ? "You can submit once the API is ready, but the first job may be faster after the warm cache chip turns ready."
            : "Enable Auto warm on downloaded databases if you want the next start to prepare node-local cache automatically.",
      },
    ],
    [
      aksStartSamples,
      aksStartSeconds,
      apiReadySeconds,
      autoWarmupDbCount,
      clusterName,
      isMeasured,
      openapiSeconds,
      totalHigh,
      totalLow,
    ],
  );

  const activeTip = tips[tipIndex];
  const Icon = activeTip.icon;

  return (
    <div
      style={{
        border: "1px solid var(--border-weak)",
        borderRadius: 10,
        background: "rgba(122, 167, 255, 0.08)",
        padding: "10px 12px",
        display: "grid",
        gridTemplateColumns: "auto 1fr",
        gap: 10,
        alignItems: "start",
      }}
      aria-live="polite"
    >
      <span
        style={{
          width: 26,
          height: 26,
          borderRadius: 8,
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          color: "var(--accent)",
          background: "rgba(122, 167, 255, 0.10)",
          flexShrink: 0,
        }}
      >
        <Icon size={15} strokeWidth={1.6} />
      </span>
      <div style={{ display: "grid", gap: 4, minWidth: 0 }}>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            gap: 12,
            flexWrap: "wrap",
            alignItems: "baseline",
          }}
        >
          <strong style={{ fontSize: 12 }}>{activeTip.title}</strong>
          <span
            className="muted"
            style={{ fontSize: 11, display: "inline-flex", alignItems: "baseline", gap: 6 }}
          >
            <span>
              elapsed {formatDuration(elapsedSeconds)} · estimated API readiness{" "}
              {formatDuration(apiRemaining)}
            </span>
            {!isMeasured && (
              <span
                title="No start has been observed yet — based on built-in defaults. The estimate sharpens after the first measured start."
                style={{
                  fontSize: 10,
                  lineHeight: 1.6,
                  padding: "0 6px",
                  borderRadius: 999,
                  border: "1px solid var(--border-weak)",
                  color: "var(--muted)",
                  whiteSpace: "nowrap",
                }}
              >
                estimate
              </span>
            )}
          </span>
        </div>
        <span className="muted" style={{ fontSize: 12, lineHeight: 1.45 }}>
          {activeTip.body}
        </span>
      </div>
    </div>
  );
}
