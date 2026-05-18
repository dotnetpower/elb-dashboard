/**
 * L2 — Shared loading-row skeleton.
 *
 * Replaces ad-hoc `[1,2,3].map(i => <div className="skeleton skeleton-line">)`
 * blocks scattered across pages. Centralising it makes the spacing/height
 * tokens consistent and lets future a11y improvements (e.g. role="status",
 * `prefers-reduced-motion`) land in one place.
 */
import { useId } from "react";

export interface RowSkeletonProps {
  /** Number of skeleton rows to render. */
  count?: number;
  /** Row height in px. */
  height?: number;
  /** Vertical gap between rows in px. */
  gap?: number;
  /** Optional `aria-label` for screen readers. */
  label?: string;
}

export function RowSkeleton({
  count = 3,
  height = 40,
  gap = 8,
  label = "Loading…",
}: RowSkeletonProps) {
  const baseId = useId();
  return (
    <div
      role="status"
      aria-live="polite"
      aria-label={label}
      style={{ display: "flex", flexDirection: "column", gap }}
    >
      {Array.from({ length: count }, (_, idx) => (
        <div
          key={`${baseId}-${idx}`}
          className="skeleton skeleton-line"
          style={{ height, width: "100%" }}
          aria-hidden
        />
      ))}
    </div>
  );
}
