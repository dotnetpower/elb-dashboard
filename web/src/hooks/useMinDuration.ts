import { useEffect, useRef, useState } from "react";

/**
 * Keep a boolean visible for at least `minMs` after it goes true, so very
 * short-lived "loading" / "fetching" flags still produce a perceptible UI
 * change. Returns the held-true value, never lengthens a false → false
 * transition.
 *
 * Used by `MonitorCard` so the refresh shimmer bar is still noticeable when
 * the backend responds in ~50–200 ms (less than one animation cycle).
 */
export function useMinDuration(active: boolean, minMs: number): boolean {
  const [held, setHeld] = useState(active);
  const releaseAtRef = useRef<number>(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (active) {
      releaseAtRef.current = Date.now() + minMs;
      setHeld(true);
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      return;
    }
    const remaining = releaseAtRef.current - Date.now();
    if (remaining <= 0) {
      setHeld(false);
      return;
    }
    timerRef.current = setTimeout(() => {
      setHeld(false);
      timerRef.current = null;
    }, remaining);
    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [active, minMs]);

  return held;
}
