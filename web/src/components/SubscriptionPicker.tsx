import { useEffect, useMemo, type CSSProperties } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";

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

  // `value` was saved by the wizard but the current credential can no longer
  // see that subscription (e.g. the developer switched `az` profiles, or the
  // subscription was moved to another tenant). The select would otherwise
  // silently render a blank option — surface the mismatch explicitly so the
  // workspace diagnostics banner can pick it up via the same query.
  const invalidValue = useMemo(() => {
    if (!value) return false;
    if (!query.data) return false; // still loading or errored — do not flag yet
    return !query.data.some((s) => s.subscriptionId === value);
  }, [value, query.data]);

  if (compact) {
    return (
      <div
        className={`cfg-chip${invalidValue ? " cfg-chip--invalid" : ""}`}
        style={style}
        title={
          invalidValue
            ? `Saved subscription ${value.slice(0, 8)}… is not visible to your current Azure credential. Pick another, or click Reset workspace in the diagnostics banner.`
            : undefined
        }
      >
        <span className="lbl">{label}</span>
        <select
          value={value}
          disabled={query.isLoading || query.isError}
          onChange={(e) => onChange(e.target.value)}
        >
          {query.isLoading && <option value="">Loading…</option>}
          {query.isError && <option value="">Error</option>}
          {invalidValue && (
            <option value={value}>
              {value.slice(0, 8)}… (not visible)
            </option>
          )}
          {query.data?.map((s: ArmSubscription) => (
            <option key={s.subscriptionId} value={s.subscriptionId}>
              {s.displayName}
            </option>
          ))}
        </select>
        {invalidValue && (
          <AlertTriangle
            size={12}
            strokeWidth={1.5}
            className="cfg-chip__warn"
            aria-hidden="true"
          />
        )}
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
        {invalidValue && (
          <option value={value}>
            {value} (not visible to current credential)
          </option>
        )}
        {query.data?.map((s: ArmSubscription) => (
          <option key={s.subscriptionId} value={s.subscriptionId}>
            {s.displayName} ({s.subscriptionId.slice(0, 8)}…) · {s.state}
          </option>
        ))}
      </select>
      {invalidValue && (
        <div className="muted" style={{ fontSize: 12, marginTop: 4, color: "var(--warning)" }}>
          Saved subscription is not visible to the current Azure credential. Pick another above
          or reset the workspace.
        </div>
      )}
      {query.isError && (
        <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
          {(query.error as Error).message}. Make sure you granted ARM consent on sign-in.
        </div>
      )}
    </label>
  );
}
