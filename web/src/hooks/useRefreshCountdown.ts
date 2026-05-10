import { useState, useEffect } from "react";

/**
 * Returns seconds remaining until the next refetch fires.
 * Resets every time `dataUpdatedAt` changes (i.e. data was fetched).
 *
 * @param dataUpdatedAt - query.dataUpdatedAt from TanStack Query (epoch ms, 0 if never)
 * @param intervalMs    - the refetchInterval in ms (or null/0 to disable)
 */
export function useRefreshCountdown(
  dataUpdatedAt: number,
  intervalMs: number | null | undefined,
): number | null {
  const [remaining, setRemaining] = useState<number | null>(null);

  useEffect(() => {
    if (!intervalMs || !dataUpdatedAt) {
      setRemaining(null);
      return;
    }

    const tick = () => {
      const elapsed = Date.now() - dataUpdatedAt;
      const left = Math.max(0, Math.ceil((intervalMs - elapsed) / 1000));
      setRemaining(left);
    };

    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [dataUpdatedAt, intervalMs]);

  return remaining;
}
