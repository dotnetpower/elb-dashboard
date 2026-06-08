// Pure node-count helpers for the cluster workload-pool ScalePanel slider +
// number input. Kept separate from the React component so they can be unit
// tested without a DOM (the repo's frontend tests are logic-level `.test.ts`,
// there is no component-render harness).

// Hard ceiling kept just below the backend `_MAX_SCALE_NODE_COUNT` (100). The
// usable slider max is derived per-cluster (see `sliderMaxFor`).
export const SCALE_NODE_HARD_MAX = 100;

/**
 * Per-cluster slider maximum: give headroom above the current size (at least
 * 16) without overwhelming the track for a small cluster, capped at the backend
 * hard max so the slider can never request a count the backend rejects.
 */
export function sliderMaxFor(current: number): number {
  const safeCurrent = Number.isFinite(current) ? Math.max(1, current) : 1;
  return Math.min(SCALE_NODE_HARD_MAX, Math.max(16, safeCurrent * 2));
}

/**
 * Clamp a (possibly NaN / fractional / out-of-range) node count into
 * `[1, max]` as an integer. Guards the number input where the user can type
 * anything.
 */
export function clampNodeCount(value: number, max: number): number {
  if (!Number.isFinite(value)) return 1;
  return Math.max(1, Math.min(max, Math.round(value)));
}
