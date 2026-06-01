/**
 * Live progress + ETA helpers for the BLAST prepare-db copy flow.
 *
 * Pure functions only — the React-specific monotonic clamp lives in
 * `BlastDbRow`. Splitting the math out keeps it unit-testable and keeps the
 * row component focused on rendering.
 *
 * Contract: the prepare-db pipeline writes `copy_status.success` (files whose
 * server-side copy reached terminal `success`) monotonically. The UI projects
 * the remaining time from observed throughput (`success / elapsed`) rather
 * than the static catalog estimate, so the figure tightens as the copy runs.
 */

/** Format a duration in seconds into a compact `12s` / `7m` / `1h 5m` label. */
export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const minutes = Math.round(s / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remMinutes = minutes % 60;
  return remMinutes ? `${hours}h ${remMinutes}m` : `${hours}h`;
}

/**
 * Project the remaining copy time from elapsed seconds and the
 * copied/total file ratio.
 *
 * Returns:
 *   - `""` when there is nothing meaningful to show (no total, or already done)
 *   - `"estimating…"` while throughput is not yet stable (no files copied yet
 *     or fewer than ~5 s elapsed)
 *   - `"~7m left"` once enough progress exists to extrapolate
 */
export function formatEta(
  elapsedSeconds: number,
  copied: number,
  total: number,
): string {
  if (total <= 0) return "";
  if (copied >= total) return "";
  if (copied <= 0 || elapsedSeconds < 5) return "estimating…";
  const remainingSeconds = (elapsedSeconds * (total - copied)) / copied;
  return `~${formatDuration(remainingSeconds)} left`;
}
