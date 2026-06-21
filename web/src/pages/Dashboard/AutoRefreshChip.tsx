import { RefreshCw } from "lucide-react";

import { AUTO_REFRESH_OPTIONS, useAutoRefresh } from "@/hooks/useAutoRefresh";
import { RefreshRing } from "@/components/RefreshRing";

/**
 * Compact dropdown for the global dashboard auto-refresh interval.
 * Styled as a `cfg-chip` so it lines up visually with the Subscription /
 * Workload RG pickers next to it.
 */
export function AutoRefreshChip() {
  const { intervalMs, setIntervalMs, secondsToRefresh } = useAutoRefresh();
  return (
    <label
      className="cfg-chip"
      title="How often dashboard cards refetch from Azure"
      style={{ cursor: "pointer" }}
    >
      <span
        className="lbl"
        style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
      >
        <RefreshCw size={11} strokeWidth={1.5} />
        Auto-refresh
      </span>
      <select
        value={intervalMs}
        onChange={(e) => setIntervalMs(Number(e.target.value))}
        aria-label="Auto-refresh interval"
      >
        {AUTO_REFRESH_OPTIONS.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>
      <RefreshRing seconds={secondsToRefresh} total={Math.round(intervalMs / 1000)} />
    </label>
  );
}
