import { useEffect, type CSSProperties } from "react";
import { useQuery } from "@tanstack/react-query";

import { armProxyApi, type ArmSubscription } from "@/api/endpoints";

interface Props {
  value: string;
  onChange: (subscriptionId: string) => void;
  label?: string;
  compact?: boolean;
  style?: CSSProperties;
}

export function SubscriptionPicker({ value, onChange, label = "Subscription", compact = false, style }: Props) {
  const query = useQuery({
    queryKey: ["arm-subscriptions"],
    queryFn: armProxyApi.listSubscriptions,
    staleTime: 5 * 60_000,
  });

  // Auto-select the first subscription when nothing is chosen yet.
  useEffect(() => {
    if (!value && query.data && query.data.length > 0) {
      onChange(query.data[0].subscriptionId);
    }
  }, [value, query.data, onChange]);

  if (compact) {
    return (
      <div className="cfg-chip" style={style}>
        <span className="lbl">{label}</span>
        <select
          value={value}
          disabled={query.isLoading || query.isError}
          onChange={(e) => onChange(e.target.value)}
        >
          {query.isLoading && <option value="">Loading…</option>}
          {query.isError && <option value="">Error</option>}
          {query.data?.map((s: ArmSubscription) => (
            <option key={s.subscriptionId} value={s.subscriptionId}>
              {s.displayName}
            </option>
          ))}
        </select>
      </div>
    );
  }

  return (
    <label>
      <span className="glass-label">{label}</span>
      <select
        className="glass-input"
        value={value}
        disabled={query.isLoading || query.isError}
        onChange={(e) => onChange(e.target.value)}
      >
        {query.isLoading && <option value="">Loading…</option>}
        {query.isError && <option value="">Failed to load</option>}
        {query.data && query.data.length === 0 && (
          <option value="">No subscriptions visible to this account</option>
        )}
        {query.data?.map((s: ArmSubscription) => (
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
