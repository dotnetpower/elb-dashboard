# AKS card: live real-time auto-stop countdown

## Motivation

When idle auto-stop is enabled on an AKS cluster, the expanded cluster card
only showed the remaining time inside the amber pre-stop banner, and only once
the evaluator reached the `warn` verdict (~15 min before stop). Even then the
displayed value was a **static snapshot** of `seconds_until_stop` that refreshed
only on the 60-second status poll — it did not tick down. For the calmer `keep`
verdict (auto-stop armed but more than ~15 min left) no remaining time was shown
at all, so an operator could not tell how long the cluster would keep running.

## User-facing change

- The remaining time until auto-stop now **ticks down in real time** (once per
  second) whenever auto-stop is armed with a known deadline (`keep`, `warn`, or
  `stop` verdict) on a running, caller-owned cluster.
- A new always-visible inline countdown ("Stops in 44m 58s", with a clock icon
  and the projected stop time on hover) appears in the toggle row for the calm
  `keep` state, so the remaining time is visible long before the amber banner
  appears.
- The amber pre-stop banner now shows the same live ticking value instead of a
  value frozen until the next poll.
- When the local countdown reaches zero the panel issues a single status refetch
  so it converges to the real verdict immediately instead of waiting up to a
  full 60-second poll cycle.

The 60-second status poll cadence is unchanged — the live countdown is computed
client-side from the backend's projected `next_stop_at`, and each poll resyncs
the anchor so local drift never exceeds ~1 second. No extra backend load.

## API / IaC diff summary

- Frontend only: `web/src/components/ClusterItem/AutoStopPanel.tsx`.
  - Added `useLiveSecondsUntil(nextStopAt)` hook — ticks every second from the
    projected deadline, returns `null` when no deadline is armed (interval idle).
  - Added `armedWithDeadline` + `liveSeconds` derivation and a zero-crossing
    refetch nudge.
  - Warn banner and a new inline `keep`-state chip both render `liveSeconds`
    (tabular-nums to avoid width jitter).
- No backend, route, schema, or IaC change. The status endpoint already returned
  `next_stop_at` and `seconds_until_stop` for `keep`/`warn`/`stop` verdicts.

## Validation evidence

- TypeScript language-server diagnostics for the changed file: no errors.
- `npx eslint src/components/ClusterItem/AutoStopPanel.tsx` → clean.
- Hooks (`useLiveSecondsUntil`, zero-nudge `useEffect`) are invoked
  unconditionally before the read-only early return (rules-of-hooks satisfied).
- Consumer check: `AutoStopPanel` is consumed only by
  `web/src/components/ClusterItem/ClusterItem.tsx`; props are unchanged, so no
  caller update was required.
- Note: a repo-wide `npm run build` is currently blocked by unrelated
  in-progress edits in `web/src/pages/apiReference/ResponseViewer.tsx` (a
  different workstream); the AutoStopPanel change itself type-checks and lints
  clean in isolation.
