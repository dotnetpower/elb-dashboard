import { useEffect, useId, type CSSProperties } from "react";
import { useQuery } from "@tanstack/react-query";

interface Item {
  value: string;
  label: string;
  description?: string;
}

interface Props {
  label: string;
  value: string;
  onChange: (value: string) => void;
  /** TanStack Query key. Returning [] is fine; null means "disabled". */
  queryKey: readonly unknown[];
  /** null = disabled (e.g. dependent dropdown without a parent value). */
  fetcher: (() => Promise<Item[]>) | null;
  /** Placeholder shown when fetcher is disabled. */
  disabledPlaceholder?: string;
  /** Allow free-text entry as a fallback (renders an `Other…` option). */
  allowCustom?: boolean;
  /** Compact chip mode for the config strip. */
  compact?: boolean;
  style?: CSSProperties;
}

export function ResourcePicker({
  label,
  value,
  onChange,
  queryKey,
  fetcher,
  disabledPlaceholder = "Configure parent first",
  allowCustom = false,
  compact = false,
  style,
}: Props) {
  const reactId = useId();
  const enabled = fetcher !== null;
  const query = useQuery({
    queryKey,
    queryFn: () => fetcher!(),
    enabled,
    staleTime: 30_000,
  });

  // Auto-select first item when nothing is chosen yet.
  useEffect(() => {
    if (!enabled) return;
    if (!value && query.data && query.data.length > 0) {
      onChange(query.data[0].value);
    }
  }, [enabled, value, query.data, onChange]);

  const knownValues = new Set(query.data?.map((i) => i.value) ?? []);
  const showCustomInput = allowCustom && value && !knownValues.has(value);

  if (compact) {
    return (
      <div className="cfg-chip" style={style}>
        <span className="lbl">{label}</span>
        <select
          value={value}
          disabled={!enabled || query.isLoading}
          onChange={(e) => onChange(e.target.value)}
        >
          {!enabled && <option value="">{disabledPlaceholder}</option>}
          {enabled && query.isLoading && <option value="">Loading…</option>}
          {query.data?.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
          {allowCustom && <option value="__custom__">Other…</option>}
        </select>
      </div>
    );
  }

  return (
    <label htmlFor={reactId}>
      <span className="glass-label">{label}</span>
      {showCustomInput ? (
        <input
          id={reactId}
          className="glass-input"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          spellCheck={false}
        />
      ) : (
        <select
          id={reactId}
          className="glass-input"
          value={value}
          disabled={!enabled || query.isLoading || query.isError}
          onChange={(e) => onChange(e.target.value)}
        >
          {!enabled && <option value="">{disabledPlaceholder}</option>}
          {enabled && query.isLoading && <option value="">Loading…</option>}
          {enabled && query.isError && <option value="">Failed to load</option>}
          {enabled && query.data && query.data.length === 0 && (
            <option value="">None found in this scope</option>
          )}
          {query.data?.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
              {item.description ? ` · ${item.description}` : ""}
            </option>
          ))}
          {allowCustom && <option value="__custom__">Other…</option>}
        </select>
      )}
      {query.isError && (
        <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
          {(query.error as Error).message}
        </div>
      )}
    </label>
  );
}
