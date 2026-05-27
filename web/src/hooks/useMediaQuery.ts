import { useEffect, useState } from "react";

/**
 * Subscribe to a CSS media query. Returns `true` when the query currently
 * matches, `false` otherwise. SSR-safe (returns `false` until mount).
 *
 * Pulled into a shared hook so the responsive header (Layout + NavMoreDropdown)
 * and any future viewport-aware component share one tested implementation
 * instead of each writing its own `useEffect` + `addEventListener` block.
 *
 * @example
 *   const isCompact = useMediaQuery("(max-width: 1320px)");
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState<boolean>(() => {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    return window.matchMedia(query).matches;
  });
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia(query);
    const onChange = (event: MediaQueryListEvent) => setMatches(event.matches);
    // Sync once on mount in case the query string changed between renders.
    setMatches(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [query]);
  return matches;
}
