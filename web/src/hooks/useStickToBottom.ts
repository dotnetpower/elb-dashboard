/**
 * useStickToBottom — window-level "follow the tail" scroll behaviour.
 *
 * Responsibility: When BLAST progresses (new log lines, step transitions),
 * keep the window scrolled so the user always sees the most recent output —
 * unless the user has manually scrolled up to inspect earlier content, in
 * which case auto-scroll pauses until they return to the tail. The "tail"
 * is the bottom of the currently-active step row when an `anchorSelector`
 * is supplied (GitHub-Actions-style: follow the running step, not the page
 * bottom — the BLAST timeline always renders the still-pending steps BELOW
 * the active one, so the document bottom is a stack of empty pending rows,
 * not the live log). With no anchor present it falls back to the document
 * bottom.
 * Edit boundaries: Keep this pure window-scroll behaviour; do not depend
 * on a specific DOM container. Components opt in by calling the hook with
 * an opaque `version` value that increments whenever new content arrives,
 * and optionally an `anchorSelector` that resolves to the row to tail.
 * Key entry points: `useStickToBottom({ version, enabled, anchorSelector })`,
 * `shouldFollow`, `shouldFollowAnchor`, `anchorFollowTarget`.
 * Risky contracts: Only safe in the browser (uses `window`). The threshold
 * for "at bottom" must be generous enough to survive layout reflows and
 * sub-pixel rounding; 96 px matches typical sticky-footer heights. The
 * smooth follow relies on a ResizeObserver firing on every body-height
 * growth (each appended live-log line); dropping it reverts to the old
 * coarse, multi-second "lurch" follow driven by the debounced `version`.
 * When an anchor is present the follow/pause decision MUST be measured
 * against the anchor bottom (not the document bottom) or a small user
 * scroll after an anchor-aligned auto-scroll would be misread as
 * "scrolled away" and pause following.
 * Validation: `cd web && npm test -- useStickToBottom`.
 */
import { useEffect, useRef } from "react";

const BOTTOM_THRESHOLD_PX = 96;
/**
 * Breathing room left below the anchor's bottom edge so the latest line is
 * not flush against the viewport bottom and a sliver of the next pending
 * step stays visible as a "more is coming" cue.
 */
const ANCHOR_BOTTOM_MARGIN_PX = 24;

/**
 * Pure decision: given the current scroll geometry, is the viewport close
 * enough to the bottom that we should keep auto-following the tail?
 * Extracted so the user-control contract is unit-testable without a real
 * layout engine.
 */
export function shouldFollow(
  scrollTop: number,
  viewportHeight: number,
  documentHeight: number,
  threshold: number = BOTTOM_THRESHOLD_PX,
): boolean {
  return scrollTop + viewportHeight >= documentHeight - threshold;
}

/**
 * Pure decision for the anchor (active-step) follow mode: is the viewport
 * bottom at or past the anchor's bottom edge, within the threshold? When the
 * user scrolls up further than the threshold above the anchor bottom this
 * returns false and following pauses.
 */
export function shouldFollowAnchor(
  scrollTop: number,
  viewportHeight: number,
  anchorBottom: number,
  threshold: number = BOTTOM_THRESHOLD_PX,
): boolean {
  return scrollTop + viewportHeight >= anchorBottom - threshold;
}

/**
 * Pure target computation: the scrollTop that aligns the anchor's bottom
 * edge to `margin` px above the viewport bottom, clamped to the scrollable
 * range so we never overscroll a short document.
 */
export function anchorFollowTarget(
  anchorBottom: number,
  viewportHeight: number,
  documentHeight: number,
  margin: number = ANCHOR_BOTTOM_MARGIN_PX,
): number {
  const maxScroll = Math.max(0, documentHeight - viewportHeight);
  const target = anchorBottom - viewportHeight + margin;
  return Math.min(Math.max(0, target), maxScroll);
}

export function useStickToBottom({
  version,
  enabled = true,
  anchorSelector,
}: {
  /** Monotonically-increasing token whose change signals new tail content. */
  version: number | string;
  /** Disable when the host page is not visible (e.g. wrong tab). */
  enabled?: boolean;
  /**
   * Optional CSS selector for the element whose bottom edge is the tail to
   * follow (the active step row). When multiple match, the LAST one wins so
   * the lowest meaningful row is tailed. When omitted or unmatched, the hook
   * follows the document bottom.
   */
  anchorSelector?: string;
}): void {
  // Latest selector lives in a ref so the rAF / scroll closures below read
  // the current value without re-subscribing every render.
  const anchorSelectorRef = useRef<string | undefined>(anchorSelector);
  anchorSelectorRef.current = anchorSelector;

  // Absolute document-Y of the follow anchor's bottom edge, or null when no
  // anchor element is present (→ fall back to document-bottom follow).
  const getAnchorBottom = (): number | null => {
    if (typeof window === "undefined") return null;
    const selector = anchorSelectorRef.current;
    if (!selector) return null;
    const matches = document.querySelectorAll(selector);
    const el = matches.length > 0 ? matches[matches.length - 1] : null;
    if (!el) return null;
    return el.getBoundingClientRect().bottom + window.scrollY;
  };

  const isFollowing = (): boolean => {
    if (typeof window === "undefined") return false;
    const anchorBottom = getAnchorBottom();
    if (anchorBottom !== null) {
      return shouldFollowAnchor(window.scrollY, window.innerHeight, anchorBottom);
    }
    return shouldFollow(
      window.scrollY,
      window.innerHeight,
      document.documentElement.scrollHeight,
    );
  };

  const scrollToTail = (): void => {
    if (typeof window === "undefined") return;
    const anchorBottom = getAnchorBottom();
    const top =
      anchorBottom !== null
        ? anchorFollowTarget(
            anchorBottom,
            window.innerHeight,
            document.documentElement.scrollHeight,
          )
        : document.documentElement.scrollHeight;
    window.scrollTo({ top, behavior: "auto" });
  };
  // True until the user manually scrolls away from the bottom. Re-arms when
  // they scroll back to the bottom.
  const followingRef = useRef(true);
  // First effect run for this mount → always scroll, regardless of position
  // (handles "navigate into completed job and land at the top").
  const armedForInitialRef = useRef(true);
  const lastVersionRef = useRef<number | string | null>(null);
  // Coalesce rapid scroll requests (many ResizeObserver callbacks in one
  // frame) into a single rAF-driven scroll so we never thrash layout.
  const rafRef = useRef<number | null>(null);
  // While we are programmatically scrolling, the resulting `scroll` event
  // must not be misread as a manual scroll-away. This flag suppresses that
  // one self-induced event.
  const selfScrollingRef = useRef(false);

  // Stable scroll requester (rAF-coalesced). Lives in a ref so the effects
  // below can call it without re-subscribing when it would otherwise change
  // identity each render.
  const requestScrollRef = useRef<() => void>(() => {});
  requestScrollRef.current = () => {
    if (typeof window === "undefined") return;
    if (rafRef.current !== null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      selfScrollingRef.current = true;
      scrollToTail();
      followingRef.current = true;
      // Release the self-scroll guard after the scroll event has had a
      // chance to fire (next frame).
      requestAnimationFrame(() => {
        selfScrollingRef.current = false;
      });
    });
  };

  // Track manual scroll to toggle following on/off. Ignore the single
  // self-induced scroll event produced by our own scrollToTail().
  useEffect(() => {
    if (!enabled || typeof window === "undefined") return;
    const onScroll = () => {
      if (selfScrollingRef.current) return;
      followingRef.current = isFollowing();
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [enabled]);

  // Smooth follow: react to EVERY body-height growth (each appended live-log
  // line) rather than only the coarse, debounced `version` token. This is
  // what makes the follow feel continuous (GitHub-Actions-like) instead of
  // jumping in multi-second chunks. Only scroll when the user is still
  // anchored near the bottom.
  useEffect(() => {
    if (!enabled || typeof window === "undefined") return;
    if (typeof ResizeObserver === "undefined") return;
    const target = document.body;
    if (!target) return;
    let lastHeight = target.scrollHeight;
    const observer = new ResizeObserver(() => {
      const next = target.scrollHeight;
      if (next <= lastHeight) {
        lastHeight = next;
        return;
      }
      lastHeight = next;
      if (!followingRef.current) return;
      requestScrollRef.current();
    });
    observer.observe(target);
    return () => observer.disconnect();
  }, [enabled]);

  // On mount + every version change, force a scroll to the tail IF either
  // (a) this is the initial render (land at the tail of an already-rendered
  // completed job, where no further growth fires the ResizeObserver), or
  // (b) the user is still anchored near the tail (covers phase-transition
  // cues — where the active anchor row changes — and any growth the observer
  // missed).
  useEffect(() => {
    if (!enabled || typeof window === "undefined") return;
    if (lastVersionRef.current === version && !armedForInitialRef.current) return;
    lastVersionRef.current = version;

    const force = armedForInitialRef.current;
    armedForInitialRef.current = false;
    if (!force && !followingRef.current) return;

    requestScrollRef.current();
  }, [enabled, version]);

  // Cancel any pending rAF on unmount.
  useEffect(() => {
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
  }, []);
}
