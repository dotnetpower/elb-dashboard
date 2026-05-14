# Remove `Unlock` / `Auto` buttons from Storage Account card

## Motivation

`.github/copilot-instructions.md` §9 (Storage Network Isolation — HARD
REQUIREMENT) states every Storage account in scope is
`publicNetworkAccess: Disabled` from day 1, with no temporary public-window
toggle for control-plane traffic. The `Unlock` / `Lock` toggle and the
companion `Auto` keep-alive button on the dashboard's Storage Account card
predate that policy and now violate it on the UI surface.

The backend endpoint they called (`POST /api/monitor/storage/public-access`)
no longer exists in `api/routes/monitor.py` either — the
`set_storage_public_access` helper in `api/services/monitoring.py` is still
defined but unrouted, so the buttons were dead UI (request would 404).

## User-facing change

`web/src/components/cards/StorageCard.tsx`:

- Removed the `Unlock`/`Lock` toggle button (top-right of the card).
- Removed the `Auto` / `Auto ✓` keep-alive button (top-right of the card).
- Removed the in-card confirmation dialog ("Enable public network access?").
- Removed the toggle status messages ("Toggling…", success/error banner).
- Updated the public-access incident banner copy so it no longer says
  *"Disable after BLAST operations complete"* — it now reads
  *"Public network access is enabled — expected state is Disabled.
  Investigate and remediate."*
- Updated the BlastDbSection "Storage public access is disabled" banner so
  it no longer instructs the user to click the (removed) Unlock button.

The card still reports the current `public_network_access` value as a
read-only field, so an unexpected `Enabled` state is still surfaced
prominently (per §8 incident expectation).

## API / IaC diff summary

No backend or Bicep changes. Only `web/src/components/cards/StorageCard.tsx`
was modified.

## Validation evidence

```
$ cd web && npx tsc --noEmit -p . 2>&1 | grep StorageCard
(no output)
```

Pre-existing `ClusterCard.tsx` TS errors are untouched and unrelated.
