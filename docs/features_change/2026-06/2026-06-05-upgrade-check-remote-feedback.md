# Upgrade page: make "Check remote" give visible feedback

## Motivation

A user reported that the **Check remote** button on `/upgrade` (Self-upgrade)
"gives no response". The click handler was wired (`forceCheck` →
`POST /api/upgrade/check`), but it looked dead because:

- **No loading state.** Unlike the neighbouring Refresh button (which disables
  while `refreshing`), Check remote had no disabled/label feedback, so a slow or
  same-result check showed nothing.
- **No success feedback.** When the remote was already up to date, only the
  `latest_checked_at` timestamp changed (easy to miss), and there was no toast
  to confirm the check ran.
- **Stale dependent state.** `forceCheck` only updated the status row
  (`setStatus`), not `candidates` / `history`. The target-version picker reads
  from `candidates`, so a newly discovered release updated the "Latest
  available" stat but was **not selectable** until a separate Refresh.

## User-facing change

Check remote now:

1. Disables and relabels to "Checking…" while the request is in flight.
2. After the check, refreshes candidates + history (via `refreshAll`) so a newly
   discovered release is immediately selectable in the start picker.
3. Shows a toast summarising the outcome:
   - `Checked remote — vX.Y.Z is available to upgrade.` (newer than running)
   - `Checked remote — you are on the latest (vX.Y.Z).`
   - `Checked remote — no releases found for the configured remote.`
4. Keeps the existing throttle handling (`Check throttled — try again in Ns.`)
   via the inline `actionError` banner.

No backend change — `POST /api/upgrade/check` already returns the refreshed
state row and enforces the 15 s cooldown.

## API / IaC diff summary

- `web/src/pages/UpgradePage.tsx` — `forceCheck` rewritten to add a `checking`
  state, call `refreshAll()` after the check, and toast the outcome; the button
  gains `disabled={checking}` and a "Checking…" label. Imports `useToast`.

## Validation evidence

- `eslint src/pages/UpgradePage.tsx` — clean.
- `npm run build` — succeeds.
- `npx vitest run src/api/upgrade.test.ts` — passes (button label at rest is
  unchanged, so the e2e selector `getByRole("button", { name: "Check remote" })`
  in `scripts/e2e/scenarios/destructive-actions.mutation.spec.ts` still matches).
