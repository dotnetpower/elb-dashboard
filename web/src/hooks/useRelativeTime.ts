import { useState, useEffect } from "react";

/**
 * Returns a human-readable relative time string from a timestamp.
 * Updates every second for recent times, every 30s for older.
 */
export function useRelativeTime(timestamp: number | null | undefined): string {
  const [, setTick] = useState(0);

  useEffect(() => {
    if (!timestamp) return;
    const tick = () => {
      if (!document.hidden) setTick((t) => t + 1);
    };
    const interval = setInterval(tick, 1000);
    document.addEventListener("visibilitychange", tick);
    return () => {
      clearInterval(interval);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [timestamp]);

  if (!timestamp) return "";

  const diff = Math.floor((Date.now() - timestamp) / 1000);
  if (diff < 5) return "just now";
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}
