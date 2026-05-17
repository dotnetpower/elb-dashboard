import { ArrowRight } from "lucide-react";

export interface TopoArrowProps {
  degraded?: boolean;
}

export function TopoArrow({ degraded = false }: TopoArrowProps) {
  return (
    <div
      aria-hidden
      style={{
        position: "relative",
        height: 2,
        width: "100%",
        background: degraded
          ? "repeating-linear-gradient(90deg, var(--warning) 0 6px, transparent 6px 10px)"
          : "linear-gradient(90deg, transparent 0%, var(--text-faint) 50%, transparent 100%)",
        overflow: "visible",
      }}
    >
      <ArrowRight
        size={12}
        style={{
          position: "absolute",
          right: -2,
          top: -6,
          color: degraded ? "var(--warning)" : "var(--text-faint)",
        }}
      />
    </div>
  );
}
