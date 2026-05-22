# Dashboard cards: minimum shimmer duration on refresh

## Motivation

Every `MonitorCard` (Cluster, Storage, ACR, Terminal, Jobs …) renders a
thin shimmer bar across its top edge while React Query is fetching. On the
**initial** load this is plainly visible because the backend takes 300 ms
to a few seconds. On **refresh** the round-trip is often 50–200 ms — well
under the 1.5 s CSS sweep animation — so the bar appeared and disappeared
before a single sweep could complete, and the user couldn't tell the card
had refreshed at all.

## User-facing change

* The top-edge shimmer bar in every `MonitorCard` now stays visible for a
  minimum of **800 ms** after a refresh begins (about half of the 1.5 s
  sweep cycle), regardless of how fast the backend responds. A long
  fetch still drives the bar for its full duration as before.
* No change to the refresh button's enabled state — it re-enables as soon
  as the actual fetch completes, so the user can still click again
  immediately.
* No change to the initial-load skeleton block (three placeholder lines)
  — that block is gated on `status === "loading" && !children` and is
  already plainly visible.

## API / IaC diff summary

* **New hook**: [web/src/hooks/useMinDuration.ts](../../../web/src/hooks/useMinDuration.ts) —
  `useMinDuration(active, minMs)` returns `true` while `active` is true
  AND for at least `minMs` after it most recently went true. Pure
  client-side; no new dependencies.
* [web/src/components/MonitorCard.tsx](../../../web/src/components/MonitorCard.tsx) —
  wraps `showShimmer` in `useMinDuration(_, 800)` and uses the held
  value for the top shimmer bar only.

## Validation

* `npm run build` (in `web/`) — green.
* Manually verified that:
  * A slow fetch still drives the shimmer for the full duration.
  * A fast refetch keeps the shimmer visible for ~800 ms after click /
    auto-refresh tick.
  * The refresh button re-enables as soon as data arrives.
