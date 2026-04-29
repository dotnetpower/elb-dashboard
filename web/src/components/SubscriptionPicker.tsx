import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";

import { listSubscriptions, type SubscriptionSummary } from "@/api/arm";

interface Props {
  value: string;
  onChange: (subscriptionId: string) => void;
  label?: string;
}

export function SubscriptionPicker({ value, onChange, label = "Subscription" }: Props) {
  const query = useQuery({
    queryKey: ["arm-subscriptions"],
    queryFn: listSubscriptions,
    staleTime: 5 * 60_000,
  });

  // Auto-select the first subscription when nothing is chosen yet.
  useEffect(() => {
    if (!value && query.data && query.data.length > 0) {
      onChange(query.data[0].subscriptionId);
    }
  }, [value, query.data, onChange]);

  return (
    <label>
      <span className="glass-label">{label}</span>
      <select
        className="glass-input"
        value={value}
        disabled={query.isLoading || query.isError}
        onChange={(e) => onChange(e.target.value)}
        style={{ appearance: "auto" }}
      >
        {query.isLoading && <option value="">Loading…</option>}
        {query.isError && <option value="">Failed to load</option>}
        {query.data && query.data.length === 0 && (
          <option value="">No subscriptions visible to this account</option>
        )}
        {query.data?.map((s: SubscriptionSummary) => (
          <option key={s.subscriptionId} value={s.subscriptionId}>
            {s.displayName} ({s.subscriptionId.slice(0, 8)}…) · {s.state}
          </option>
        ))}
      </select>
      {query.isError && (
        <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
          {(query.error as Error).message}. Make sure you granted ARM consent on sign-in.
        </div>
      )}
    </label>
  );
}
