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
 * Key entry points: `useStickToBottom({ version, enabled })`.
 * Risky contracts: Only safe in the browser (uses `window`). The threshold
 * for "at bottom" must be generous enough to survive layout reflows and
 * sub-pixel rounding; 96 px matches typical sticky-footer heights.
 * Validation: `cd web && npm test -- useStickToBottom`.
 */
import { useEffect, useRef } from "react";

const BOTTOM_THRESHOLD_PX = 96;

function isAtBottom(): boolean {
  if (typeof window === "undefined") return false;
  const scrollTop = window.scrollY;
  const viewport = window.innerHeight;
  const doc = document.documentElement.scrollHeight;
  return scrollTop + viewport >= doc - BOTTOM_THRESHOLD_PX;
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

  // Track manual scroll to toggle following on/off.
  useEffect(() => {
    if (!enabled || typeof window === "undefined") return;
    const onScroll = () => {
      followingRef.current = isAtBottom();
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [enabled]);

  // On mount + every version change, scroll to the bottom IF either (a) this
  // is the initial render, or (b) the user is still anchored near the bottom.
  useEffect(() => {
    if (!enabled || typeof window === "undefined") return;
    if (lastVersionRef.current === version && !armedForInitialRef.current) return;
    lastVersionRef.current = version;

    const force = armedForInitialRef.current;
    armedForInitialRef.current = false;
    if (!force && !followingRef.current) return;

    // Wait one frame so layout reflects the just-rendered content; otherwise
    // scrollHeight reflects the previous DOM size and we under-scroll.
    const raf = requestAnimationFrame(() => {
      scrollToBottom();
      // After a forced initial scroll, treat the user as "following" so the
      // next content tick auto-scrolls too.
      followingRef.current = true;
    });
    return () => cancelAnimationFrame(raf);
  }, [enabled, version]);
}
