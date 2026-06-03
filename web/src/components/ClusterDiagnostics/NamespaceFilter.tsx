/**
 * Namespace filter bar shared by the cluster Workloads tabs.
 *
 * Single-responsibility: render the namespace `<select>` + "N shown"
 * indicator for a workload snapshot. Stateless — the selected value and the
 * derived namespace list come from `useNamespaceFilter`. Returns null when
 * there is nothing to filter, so callers can render it unconditionally.
 */
import { useMemo } from "react";

export function NamespaceFilter({
  idSuffix,
  items,
  namespaces,
  value,
  onChange,
  shown,
}: {
  idSuffix: string;
  items: { namespace: string }[];
  namespaces: string[];
  value: string;
  onChange: (value: string) => void;
  shown: number;
}) {
  const counts = useMemo(() => {
    const map = new Map<string, number>();
    for (const item of items) {
      map.set(item.namespace, (map.get(item.namespace) ?? 0) + 1);
    }
    return map;
  }, [items]);

  if (items.length === 0) return null;

  const id = `k8s-ns-filter-${idSuffix}`;
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "8px 10px",
        borderBottom: "1px solid var(--border-weak)",
        background: "var(--bg-tertiary)",
      }}
    >
      <label
        htmlFor={id}
        style={{
          fontSize: 9,
          textTransform: "uppercase",
          letterSpacing: "0.05em",
          color: "var(--text-faint)",
          fontWeight: 500,
        }}
      >
        Namespace
      </label>
      <select
        id={id}
        className="glass-input"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        style={{ fontSize: 10, padding: "3px 6px", maxWidth: 220 }}
      >
        <option value="all">All namespaces ({items.length})</option>
        {namespaces.map((ns) => (
          <option key={ns} value={ns}>
            {ns} ({counts.get(ns) ?? 0})
          </option>
        ))}
      </select>
      <span className="muted" style={{ marginLeft: "auto", fontSize: 9 }}>
        {shown} shown
      </span>
    </div>
  );
}
