/**
 * Stateless relative-time formatter.
 *
 * Unlike `useRelativeTime` (which re-renders every second from a numeric
 * timestamp), this is a pure function that accepts an ISO 8601 string and
 * returns a coarse "Xm ago" string suitable for poll-driven re-renders.
 *
 * Single-responsibility: given a timestamp, render it.
 */
export function formatRelativeTime(iso: string | undefined | null): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const diff = (Date.now() - t) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86_400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86_400)}d ago`;
}
