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

/**
 * Format a download speed from a bytes delta over an elapsed interval.
 *
 * Generic `bytes / seconds` → human rate. The caller decides the interval:
 * `BlastDbRow` passes a trailing-window delta to render an *instantaneous*
 * rate, not a whole-copy average.
 *
 * Returns:
 *   - `""` when there is nothing meaningful to show (no bytes moved, or fewer
 *     than ~5 s elapsed so the figure would be noisy)
 *   - `"42.3 MB/s"` / `"1.2 GB/s"` / `"512 KB/s"` once enough has landed
 */
export function formatSpeed(bytesDone: number, elapsedSeconds: number): string {
  if (bytesDone <= 0 || elapsedSeconds < 5) return "";
  const bytesPerSec = bytesDone / elapsedSeconds;
  const units = ["B/s", "KB/s", "MB/s", "GB/s", "TB/s"];
  let value = bytesPerSec;
  let unitIdx = 0;
  while (value >= 1024 && unitIdx < units.length - 1) {
    value /= 1024;
    unitIdx += 1;
  }
  const precision = value >= 100 || unitIdx === 0 ? 0 : 1;
  return `${value.toFixed(precision)} ${units[unitIdx]}`;
}

export const SPEED_WINDOW_MS = 45_000;

/** A single observation of cumulative bytes landed at a wall-clock instant. */
export interface SpeedSample {
  bytes: number;
  t: number;
}

/**
 * Append a new cumulative-bytes observation to a trailing-window sample list,
 * returning a NEW array (pure — never mutates the input).
 *
 * A sample is only recorded when bytes strictly advance: the backend poll
 * interval is ~10-15 s while the row re-renders ~1 Hz, so most observations
 * repeat the previous value, and a decrease (metadata reset / re-list) must
 * never push a backwards sample. After appending, samples older than
 * `windowMs` are trimmed, but the last two are always kept so a brief lull
 * does not erase the rate.
 */
export function recordSpeedSample(
  samples: readonly SpeedSample[],
  bytes: number,
  nowMs: number,
  windowMs: number = SPEED_WINDOW_MS,
): SpeedSample[] {
  const last = samples[samples.length - 1];
  const next: SpeedSample[] =
    !last || bytes > last.bytes ? [...samples, { bytes, t: nowMs }] : [...samples];
  while (next.length > 2 && nowMs - next[0].t > windowMs) {
    next.shift();
  }
  return next;
}

/**
 * Compute an instantaneous download throughput (bytes / second) from
 * trailing-window samples, or `null` when there is nothing meaningful to
 * measure.
 *
 * Uses the first↔last sample delta so the dead startup time (AKS pods
 * scheduling / pulling images / scanning NCBI S3 with zero bytes landed) is
 * excluded from the divisor. Returns `null` when there is no forward movement
 * to measure or the most recent sample is stale (nothing has advanced within
 * `windowMs`), so a stalled copy stops projecting an old rate. This is the
 * numeric basis shared by the speed label and the byte-based ETA.
 */
export function computeWindowedBytesPerSec(
  samples: readonly SpeedSample[],
  nowMs: number,
  windowMs: number = SPEED_WINDOW_MS,
): number | null {
  const first = samples[0];
  const latest = samples[samples.length - 1];
  if (!first || !latest || latest.t <= first.t) return null;
  if (nowMs - latest.t > windowMs) return null;
  const deltaBytes = latest.bytes - first.bytes;
  const deltaSeconds = (latest.t - first.t) / 1000;
  if (deltaBytes <= 0 || deltaSeconds <= 0) return null;
  return deltaBytes / deltaSeconds;
}

/**
 * Compute an instantaneous download-speed label from trailing-window samples.
 *
 * Returns `""` (hide the figure) when there is nothing to measure or fewer
 * than ~5 s of forward movement (the `formatSpeed` stability gate).
 */
export function computeWindowedSpeed(
  samples: readonly SpeedSample[],
  nowMs: number,
  windowMs: number = SPEED_WINDOW_MS,
): string {
  const first = samples[0];
  const latest = samples[samples.length - 1];
  if (!first || !latest || latest.t <= first.t) return "";
  if (nowMs - latest.t > windowMs) return "";
  const deltaBytes = latest.bytes - first.bytes;
  const deltaSeconds = (latest.t - first.t) / 1000;
  return formatSpeed(deltaBytes, deltaSeconds);
}

/**
 * Project the remaining copy time from the bytes still to land and a recent
 * throughput (bytes / second).
 *
 * Preferred over {@link formatEta} for the AKS-fanout path: file-count
 * extrapolation mis-estimates badly when the remaining files are the largest
 * `.nsq` volumes, or when a re-run finds thousands of small blobs already
 * staged (which inflates the count rate to a near-instant, bogus ETA). The
 * byte rate comes from a trailing window, so it reflects only recent movement
 * and is immune to that startup inflation.
 *
 * Returns:
 *   - `""` when there is nothing meaningful to show (no rate, no remaining
 *     bytes, or already done)
 *   - `"~28m left"` once a positive rate and positive remaining bytes exist
 */
export function formatEtaFromBytes(
  remainingBytes: number,
  bytesPerSec: number | null,
): string {
  if (!bytesPerSec || bytesPerSec <= 0) return "";
  if (remainingBytes <= 0) return "";
  return `~${formatDuration(remainingBytes / bytesPerSec)} left`;
}
