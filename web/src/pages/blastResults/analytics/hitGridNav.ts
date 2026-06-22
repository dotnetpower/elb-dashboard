/**
 * hitGridNav — pure keyboard navigation reducer for the Descriptions hits grid (#30).
 *
 * The Descriptions table (BlastHitsTable) renders an ARIA grid with row-level
 * roving tabindex. Keyboard focus moves one row per Arrow key, jumps with
 * Home/End, and — critically — interops with the #29 incremental row windowing:
 * pressing ArrowDown on the last *painted* row when more rows exist requests one
 * more window batch and advances focus into it.
 *
 * This reducer is deliberately pure (no DOM, no React) so the frontend
 * logic-only suite can exhaust the boundary cases (empty set, first/last row,
 * the load-more seam, an out-of-range starting index).
 */

export type HitGridKey = "ArrowDown" | "ArrowUp" | "Home" | "End";

export interface HitGridFocusInput {
  /** The navigation key pressed. */
  key: HitGridKey;
  /** Current focused row, 0-based; -1 means nothing is focused yet. */
  focusedRow: number;
  /** Rows currently painted (the #29 window size), 0..totalCount. */
  paintedCount: number;
  /** Total rows in the (filtered) hit set. */
  totalCount: number;
}

export interface HitGridFocusResult {
  /**
   * The row index to focus next, always within [0, totalCount-1] (or -1 when the
   * set is empty). When `loadMore` is true this may equal the current
   * `paintedCount` — the caller grows the window first, then focuses it.
   */
  nextRow: number;
  /** When true the caller should grow the row window before focusing `nextRow`. */
  loadMore: boolean;
}

const NONE: HitGridFocusResult = { nextRow: -1, loadMore: false };

/**
 * Compute the next focused row for a key press. Pure: the caller owns the
 * window-grow side effect (signalled by `loadMore`) and the actual DOM focus.
 *
 * Semantics:
 * - `ArrowDown` past the last painted row, when more rows exist, sets
 *   `loadMore=true` and advances into the next (about-to-be-painted) row.
 * - `ArrowDown` on the very last row of the set stays put (no wrap).
 * - `ArrowUp` never loads more and never goes below 0.
 * - `Home` focuses row 0. `End` focuses the last *painted* row (windowing means
 *   the absolute last row may not be painted yet — End never force-loads the
 *   whole set).
 */
export function computeHitGridFocus(input: HitGridFocusInput): HitGridFocusResult {
  const total = Math.max(0, Math.floor(input.totalCount));
  if (total <= 0) return NONE;

  const painted = Math.min(Math.max(0, Math.floor(input.paintedCount)), total);
  const lastPainted = Math.max(0, painted - 1);
  // Clamp an out-of-range / uninitialised current index into the valid window.
  const current =
    input.focusedRow < 0
      ? -1
      : Math.min(Math.floor(input.focusedRow), total - 1);

  switch (input.key) {
    case "ArrowDown": {
      if (current < 0) return { nextRow: 0, loadMore: false };
      const target = current + 1;
      if (target >= total) return { nextRow: current, loadMore: false };
      return { nextRow: target, loadMore: target >= painted };
    }
    case "ArrowUp": {
      if (current < 0) return { nextRow: 0, loadMore: false };
      return { nextRow: Math.max(0, current - 1), loadMore: false };
    }
    case "Home":
      return { nextRow: 0, loadMore: false };
    case "End":
      return { nextRow: lastPainted, loadMore: false };
    default:
      return { nextRow: current < 0 ? 0 : current, loadMore: false };
  }
}

/** The set of keys this reducer handles, for an early-out in the DOM handler. */
export function isHitGridNavKey(key: string): key is HitGridKey {
  return key === "ArrowDown" || key === "ArrowUp" || key === "Home" || key === "End";
}
