import { useRef } from "react";

/**
 * Pure clamp used by `useMonotonicPercent`. Returns the new progress floor:
 * 0 when the session reset (`sameSession=false`), otherwise the larger of the
 * previous floor and the sanitised `raw` value. Exported so the monotonic
 * contract can be unit-tested without a renderer.
 */
export function nextMonotonicFloor(
  prevFloor: number,
  rawPercent: number | null | undefined,
  sameSession: boolean,
): number {
  const raw =
    typeof rawPercent === "number" && Number.isFinite(rawPercent)
      ? Math.max(0, Math.min(100, rawPercent))
      : 0;
  if (!sameSession) {
    return raw;
  }
  return raw > prevFloor ? raw : prevFloor;
}

/**
 * Clamp a progress percentage so it never moves backwards within one logical
 * run. The long-running Azure operations in this app feed their progress bars
 * from sources that can momentarily dip even though the real work only moves
 * forward:
 *  - warmup pod logs lose the azcopy "%" the instant a shard finishes copying
 *    and starts verifying / touching memory;
 *  - the ARM provisioning banner interpolates within a step from
 *    `arm_elapsed_seconds`, whose first tick is missing;
 *  - the DB-copy bar switches its unit (per-file ↔ shard) when a transient
 *    blob-listing failure drops the `success` field.
 * Without a guard the bar visibly rewinds, which reads as "something went
 * wrong". This pins the displayed percent to the highest value seen so far.
 *
 * `resetKey` identifies the logical run (cluster name, db + generation, copy
 * session start). When it changes the floor resets so a brand-new run starts
 * from the bottom instead of being pinned at the previous run's 100%.
 * `active=false` also resets the floor so a finished / idle bar does not pin a
 * stale value into the next run.
 *
 * This is a render-time pure ref computation (no effect, no extra state) so it
 * never triggers an additional render and the clamped value is available on
 * the same paint as the raw input. The ref writes are idempotent under React's
 * StrictMode double-render (max-clamp and reset are both deterministic).
 */
export function useMonotonicPercent(
  rawPercent: number | null | undefined,
  options: { resetKey?: string | number; active?: boolean } = {},
): number {
  const { resetKey = "", active = true } = options;
  const floorRef = useRef(0);
  const keyRef = useRef<string | number>(resetKey);

  const sameSession = keyRef.current === resetKey && active;
  keyRef.current = resetKey;
  floorRef.current = nextMonotonicFloor(floorRef.current, rawPercent, sameSession);
  return floorRef.current;
}
