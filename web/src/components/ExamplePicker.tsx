import { useMemo, useState } from "react";
import { Search, Sparkles, Star } from "lucide-react";
import type { ExamplePreset } from "@/data/labToolExamples";

interface Props<T> {
  examples: ExamplePreset<T>[];
  onSelect: (values: T) => void;
  label?: string;
}

// Show the filter box only once the preset list is long enough to be worth
// searching — for a handful of presets the buttons alone are faster.
const SEARCH_THRESHOLD = 6;

export function ExamplePicker<T>({
  examples,
  onSelect,
  label = "Try an example",
}: Props<T>) {
  const [query, setQuery] = useState("");
  const showSearch = examples.length > SEARCH_THRESHOLD;
  const trimmed = query.trim().toLowerCase();
  const filtered = useMemo(() => {
    if (!trimmed) return examples;
    return examples.filter(
      (ex) =>
        ex.label.toLowerCase().includes(trimmed) ||
        (ex.description ?? "").toLowerCase().includes(trimmed),
    );
  }, [examples, trimmed]);

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        flexWrap: "wrap",
        marginBottom: 16,
      }}
    >
      <span
        className="muted"
        style={{
          fontSize: 11,
          display: "inline-flex",
          alignItems: "center",
          gap: 4,
        }}
      >
        <Sparkles size={12} /> {label}
      </span>
      {showSearch && (
        <label className="example-picker__search">
          <Search size={12} strokeWidth={1.5} aria-hidden="true" />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter examples…"
            aria-label="Filter examples"
            spellCheck={false}
          />
        </label>
      )}
      {filtered.map((ex) => (
        <button
          key={ex.id}
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={() => onSelect(ex.values)}
          title={ex.description}
          style={{
            position: "relative",
            borderColor: ex.recommended ? "rgba(122,167,255,0.35)" : undefined,
          }}
        >
          {ex.label}
          {ex.recommended && (
            <Star
              size={9}
              fill="var(--accent)"
              stroke="var(--accent)"
              style={{ marginLeft: 2 }}
            />
          )}
        </button>
      ))}
      {showSearch && filtered.length === 0 && (
        <span className="muted" style={{ fontSize: 11 }}>
          No examples match “{query.trim()}”.
        </span>
      )}
    </div>
  );
}
