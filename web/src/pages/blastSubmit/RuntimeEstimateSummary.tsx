import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Clock3 } from "lucide-react";

import { blastApi } from "@/api/blast";
import type { BlastRuntimeEstimateRequest } from "@/api/endpoints";

interface RuntimeEstimateSummaryProps {
  input: BlastRuntimeEstimateRequest | null;
}

function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
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

  const data = query.data;
  const runtime = data?.available
    ? runtimeEstimateLabel(data.estimate_seconds, data.low_seconds, data.high_seconds)
    : query.isLoading
      ? "Calculating…"
      : `Collecting baseline (${data?.sample_count ?? 0}/${data?.required_samples ?? 3})`;
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
      title="Evidence-based estimate from comparable completed jobs. It never changes submission or blocks a run."
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
      {data?.available && (
        <div className="muted" style={{ fontSize: 10, marginTop: 4 }}>
          {data.sample_count} comparable runs · {data.confidence} confidence
        </div>
      )}
    </div>
  );
}