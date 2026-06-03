/**
 * useStickToBottom — window-level "follow the tail" scroll behaviour.
 *
 * Responsibility: When BLAST progresses (new log lines, step transitions),
 * keep the window scrolled to the bottom so the user always sees the most
 * recent output — unless the user has manually scrolled up to inspect
 * earlier content, in which case auto-scroll pauses until they return to
 * the bottom.
 * Edit boundaries: Keep this pure window-scroll behaviour; do not depend
 * on a specific DOM container. Components opt in by calling the hook with
 * an opaque `version` value that increments whenever new content arrives.
 * Key entry points: `useStickToBottom({ version, enabled })`,
 * `shouldFollow`.
 * Risky contracts: Only safe in the browser (uses `window`). The threshold
 * for "at bottom" must be generous enough to survive layout reflows and
 * sub-pixel rounding; 96 px matches typical sticky-footer heights. The
 * smooth follow relies on a ResizeObserver firing on every body-height
 * growth (each appended live-log line); dropping it reverts to the old
 * coarse, multi-second "lurch" follow driven by the debounced `version`.
 * Validation: `cd web && npm test -- useStickToBottom`.
 */
import { useEffect, useRef } from "react";

const BOTTOM_THRESHOLD_PX = 96;

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

function isAtBottom(): boolean {
  if (typeof window === "undefined") return false;
  return shouldFollow(
    window.scrollY,
    window.innerHeight,
    document.documentElement.scrollHeight,
  );
}

function scrollToBottom(): void {
  if (typeof window === "undefined") return;
  window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "auto" });
}

export function useStickToBottom({
  version,
  enabled = true,
}: {
  /** Monotonically-increasing token whose change signals new tail content. */
  version: number | string;
  /** Disable when the host page is not visible (e.g. wrong tab). */
  enabled?: boolean;
}): void {
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
      scrollToBottom();
      followingRef.current = true;
      // Release the self-scroll guard after the scroll event has had a
      // chance to fire (next frame).
      requestAnimationFrame(() => {
        selfScrollingRef.current = false;
      });
    });
  };

  // Track manual scroll to toggle following on/off. Ignore the single
  // self-induced scroll event produced by our own scrollToBottom().
  useEffect(() => {
    if (!enabled || typeof window === "undefined") return;
    const onScroll = () => {
      if (selfScrollingRef.current) return;
      followingRef.current = isAtBottom();
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

  // On mount + every version change, force a scroll to the bottom IF either
  // (a) this is the initial render (land at the tail of an already-rendered
  // completed job, where no further growth fires the ResizeObserver), or
  // (b) the user is still anchored near the bottom (covers phase-transition
  // cues and any growth the observer missed).
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
