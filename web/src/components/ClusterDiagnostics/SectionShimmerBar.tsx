import { useMinDuration } from "@/hooks/useMinDuration";

/**
 * Thin top-edge shimmer bar used by the cluster diagnostics sub-cards
 * (Node Resources / Nodes / Active Pods) to mirror the `MonitorCard`
 * refresh hint. Without it, refetching a section after its initial load
 * updates the data silently — the user reads that as "Refresh All did
 * nothing" because the existing per-section indicator only reacts to
 * `isLoading` (initial mount), not `isFetching` (background refresh).
 *
 * The parent card must be `position: relative; overflow: hidden` so the
 * bar clips correctly at the rounded corners.
 */
export function SectionShimmerBar({ active }: { active: boolean }) {
  // Hold visible ≥800 ms so a fast (~100 ms) refetch still produces a
  // perceptible sweep — same threshold MonitorCard uses.
  const visible = useMinDuration(active, 800);
  if (!visible) return null;
  return (
    <div
      style={{
        position: "absolute",
        top: 0,
        left: 0,
        right: 0,
        height: 2,
        background: "rgba(122,167,255,0.15)",
        overflow: "hidden",
        zIndex: 1,
        pointerEvents: "none",
      }}
    >
      <div
        style={{
          width: "40%",
          height: "100%",
          background:
            "linear-gradient(90deg, transparent, var(--accent), transparent)",
          animation: "shimmer 1.5s ease-in-out infinite",
        }}
      />
    </div>
  );
}
