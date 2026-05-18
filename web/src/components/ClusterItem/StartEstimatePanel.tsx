import { Clock3, DatabaseZap, Lightbulb, Network } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

const OBSERVED_AKS_START_SECONDS = 235;
const OBSERVED_OPENAPI_DEPLOY_SECONDS = 31;
const WARMUP_FIRST_DB_LOW_SECONDS = 10 * 60;
const WARMUP_FIRST_DB_HIGH_SECONDS = 25 * 60;
const WARMUP_EXTRA_DB_LOW_SECONDS = 6 * 60;
const WARMUP_EXTRA_DB_HIGH_SECONDS = 15 * 60;

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))} sec`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} hr ${rest} min` : `${hours} hr`;
}

function warmupRangeSeconds(dbCount: number): [number, number] {
  if (dbCount <= 0) return [0, 0];
  return [
    WARMUP_FIRST_DB_LOW_SECONDS + (dbCount - 1) * WARMUP_EXTRA_DB_LOW_SECONDS,
    WARMUP_FIRST_DB_HIGH_SECONDS + (dbCount - 1) * WARMUP_EXTRA_DB_HIGH_SECONDS,
  ];
}

export function StartEstimatePanel({
  clusterName,
  autoWarmupDbCount,
  startedAt,
}: {
  clusterName: string;
  autoWarmupDbCount: number;
  startedAt: number | null;
}) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, []);

  const elapsedSeconds = startedAt
    ? Math.max(0, Math.floor((now - startedAt) / 1_000))
    : 0;
  const apiReadySeconds = OBSERVED_AKS_START_SECONDS + OBSERVED_OPENAPI_DEPLOY_SECONDS;
  const [warmupLow, warmupHigh] = warmupRangeSeconds(autoWarmupDbCount);
  const totalLow = apiReadySeconds + warmupLow;
  const totalHigh = apiReadySeconds + warmupHigh;
  const apiRemaining = Math.max(0, apiReadySeconds - elapsedSeconds);
  const tipIndex = Math.floor(elapsedSeconds / 12) % 4;

  const tips = useMemo(
    () => [
      {
        icon: Clock3,
        title: "Startup estimate",
        body: `Last observed AKS start took ${formatDuration(
          OBSERVED_AKS_START_SECONDS,
        )}. OpenAPI usually adds about ${formatDuration(
          OBSERVED_OPENAPI_DEPLOY_SECONDS,
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
        title: "What is happening now",
        body: `Nodes come back first, then the OpenAPI service is checked, then warmup jobs can touch database files on each node. ${clusterName} will keep polling while that catches up.`,
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
    [apiReadySeconds, autoWarmupDbCount, clusterName, totalHigh, totalLow],
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
          <span className="muted" style={{ fontSize: 11 }}>
            elapsed {formatDuration(elapsedSeconds)} · API ready in about{" "}
            {formatDuration(apiRemaining)}
          </span>
        </div>
        <span className="muted" style={{ fontSize: 12, lineHeight: 1.45 }}>
          {activeTip.body}
        </span>
      </div>
    </div>
  );
}
