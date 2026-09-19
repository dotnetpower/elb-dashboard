import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Clock3 } from "lucide-react";

import { blastApi } from "@/api/blast";
import type { BlastRuntimeEstimateRequest } from "@/api/endpoints";

interface RuntimeEstimateSummaryProps {
  input: BlastRuntimeEstimateRequest | null;
}

interface RuntimeEstimateStatusInput {
  available?: boolean;
  reason?: string | null;
  sampleCount?: number;
  requiredSamples?: number;
  isLoading: boolean;
  isError: boolean;
  inputPending: boolean;
  estimateSeconds?: number | null;
  lowSeconds?: number | null;
  highSeconds?: number | null;
}

function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const roundedSeconds = Math.max(1, Math.round(seconds));
  if (roundedSeconds < 60) return `${roundedSeconds}s`;
  if (roundedSeconds < 3_600) {
    return `${Math.min(59, Math.round(roundedSeconds / 60))}m`;
  }
  const totalMinutes = Math.round(roundedSeconds / 60);
  const hours = Math.floor(totalMinutes / 60);
  return `${hours}h ${totalMinutes % 60}m`;
}

export function runtimeEstimateLabel(
  estimateSeconds: number | null | undefined,
  lowSeconds: number | null | undefined,
  highSeconds: number | null | undefined,
): string {
  const estimate = formatDuration(estimateSeconds);
  if (estimate === "—") return estimate;
  if (lowSeconds == null || highSeconds == null) return estimate;
  return `${estimate} (${formatDuration(lowSeconds)}–${formatDuration(highSeconds)})`;
}

export function runtimeEstimateStatusLabel({
  available,
  reason,
  sampleCount = 0,
  requiredSamples = 3,
  isLoading,
  isError,
  inputPending,
  estimateSeconds,
  lowSeconds,
  highSeconds,
}: RuntimeEstimateStatusInput): string {
  if (inputPending || isLoading) return "Calculating…";
  if (isError || reason === "estimate_unavailable") return "Estimate unavailable";
  if (available) return runtimeEstimateLabel(estimateSeconds, lowSeconds, highSeconds);
  return `Collecting baseline (${sampleCount}/${requiredSamples})`;
}

export function runtimeComputeBasisLabel(
  input: BlastRuntimeEstimateRequest,
  pricing?: { source?: string | null; priced_as_of?: string | null } | null,
): string {
  const nodes = `${input.node_count} ${input.node_count === 1 ? "node" : "nodes"}`;
  const basis = pricing?.source === "live"
    ? "live on-demand rate"
    : pricing?.source === "static"
      ? `static on-demand estimate${pricing.priced_as_of ? ` (${pricing.priced_as_of})` : ""}`
      : "on-demand pricing assumption";
  return `${input.node_sku} × ${nodes} · ${basis}`;
}

export function RuntimeEstimateSummary({ input }: RuntimeEstimateSummaryProps) {
  const [debounced, setDebounced] = useState(input);
  useEffect(() => {
    const timeout = window.setTimeout(() => setDebounced(input), 750);
    return () => window.clearTimeout(timeout);
  }, [input]);

  const query = useQuery({
    queryKey: ["blast-runtime-estimate", debounced],
    queryFn: () => blastApi.estimateRuntimeCost(debounced!),
    enabled: Boolean(debounced),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
  if (!input) return null;

  const inputPending = input !== debounced;
  const data = inputPending ? undefined : query.data;
  const runtime = runtimeEstimateStatusLabel({
    available: data?.available,
    reason: data?.reason,
    sampleCount: data?.sample_count,
    requiredSamples: data?.required_samples,
    isLoading: query.isLoading,
    isError: query.isError,
    inputPending,
    estimateSeconds: data?.estimate_seconds,
    lowSeconds: data?.low_seconds,
    highSeconds: data?.high_seconds,
  });
  const cost = data?.available && data.estimated_cost_usd != null
    ? `$${data.estimated_cost_usd.toFixed(2)}`
    : "—";

  return (
    <div
      style={{
        marginTop: "var(--space-2)",
        paddingTop: "var(--space-2)",
        borderTop: "1px solid var(--glass-border)",
      }}
      title="Evidence-based estimate from comparable completed jobs. On-demand rates exclude Spot and reservation discounts. It never changes submission or blocks a run."
    >
      <div className="bsl-rail__kv">
        <span className="bsl-rail__k" style={{ display: "inline-flex", gap: 4 }}>
          <Clock3 size={11} /> Runtime estimate
        </span>
        <span className="bsl-rail__v">{runtime}</span>
      </div>
      <div className="bsl-rail__kv">
        <span className="bsl-rail__k">Compute estimate</span>
        <span className="bsl-rail__v">{cost}</span>
      </div>
      <div className="runtime-estimate-basis">
        {runtimeComputeBasisLabel(input, data?.pricing)}
      </div>
      {data?.available && (
        <div className="muted" style={{ fontSize: 10, marginTop: 4 }}>
          {data.sample_count} comparable runs · {data.confidence} confidence
        </div>
      )}
    </div>
  );
}