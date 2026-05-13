import { Sparkles, Star } from "lucide-react";
import type { ExamplePreset } from "@/data/labToolExamples";

interface Props<T> {
  examples: ExamplePreset<T>[];
  onSelect: (values: T) => void;
  label?: string;
}

export function ExamplePicker<T>({
  examples,
  onSelect,
  label = "Try an example",
}: Props<T>) {
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
      {examples.map((ex) => (
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
    </div>
  );
}
