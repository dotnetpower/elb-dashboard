// Pure helpers for the AKS idle auto-stop live countdown. Kept out of the
// React component so the timing-sensitive logic (clock-skew-safe remaining
// seconds + the display formatter) is unit-testable without a DOM test
// environment (the web suite is node-only — no jsdom / testing-library).
//
// Responsibility: derive and format the seconds remaining until an armed
// auto-stop fires, anchored on the backend's relative `seconds_until_stop`
// snapshot rather than an absolute wall-clock deadline.
// Edit boundaries: pure functions only — no React, no I/O, no Date.now()
// captured at module scope (callers pass `nowMs`).
// Key entry points: `computeRemainingSeconds`, `formatCountdown`,
// `formatCoarseRemaining`.
// Risky contracts: `computeRemainingSeconds` must clamp into
// `[0, baselineSeconds]` so a backward client-clock jump cannot inflate the
// displayed value above the server's snapshot.
// Validation: web/src/components/ClusterItem/autoStopCountdown.test.ts.

// Seconds remaining until auto-stop, anchored on the backend's relative
// `baselineSeconds` (its `seconds_until_stop` snapshot) plus the client time
// at which that snapshot arrived (`anchorMs`). Both `anchorMs` and `nowMs`
// come from the same client clock, so the elapsed difference is immune to
// client/server clock skew. Clamped to `[0, baselineSeconds]`: the lower
// bound stops the countdown at zero, the upper bound prevents a backward
// clock jump (`nowMs < anchorMs`) from showing more time than the server
// projected.
export function computeRemainingSeconds(
  baselineSeconds: number,
  anchorMs: number,
  nowMs: number,
): number {
  if (!Number.isFinite(baselineSeconds) || baselineSeconds <= 0) {
    return 0;
  }
  const elapsedSeconds = (nowMs - anchorMs) / 1000;
  const remaining = baselineSeconds - elapsedSeconds;
  return Math.min(baselineSeconds, Math.max(0, Math.round(remaining)));
}

// Display formatter that ALWAYS keeps a seconds field so a ticking live
// countdown visibly changes every second even past the one-hour mark.
// Minutes/seconds are zero-padded so the width stays stable alongside
// `font-variant-numeric: tabular`.
export function formatCountdown(seconds: number): string {
  const total = Number.isFinite(seconds) ? Math.max(0, Math.round(seconds)) : 0;
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  if (h > 0) {
    return `${h}h ${pad(m)}m ${pad(s)}s`;
  }
  if (m > 0) {
    return `${m}m ${pad(s)}s`;
  }
  return `${s}s`;
}

// Coarse, minute-granularity remaining text for screen readers. The
// per-second `formatCountdown` value lives in an `aria-hidden` node so it
// does not flood an `aria-live` region; this string updates only when the
// backend snapshot refreshes (~once per poll), so a polite live region can
// announce it without spamming. Returns e.g. "about 15 minutes",
// "about 1 minute", or "less than a minute".
export function formatCoarseRemaining(seconds: number): string {
  const total = Number.isFinite(seconds) ? Math.max(0, Math.round(seconds)) : 0;
  if (total < 60) {
    return "less than a minute";
  }
  const minutes = Math.round(total / 60);
  return minutes === 1 ? "about 1 minute" : `about ${minutes} minutes`;
}

