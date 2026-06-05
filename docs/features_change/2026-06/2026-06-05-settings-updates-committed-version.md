---
title: Settings → Updates shows the committed build version and always allows a check
description: The Updates "Current version" row now shows the full committed
  build version (release + build number + commit) instead of only the release
  version, and the "Check now" button is no longer disabled when the persisted
  state row has not yet recorded a git remote.
tags:
  - ui
  - release
---

# Settings → Updates: committed version + always-enabled check

## Motivation
Two papercuts in the Settings → Updates section:

1. **Current version** showed only the backend release version (`vA.B.0` from
   `upgrade.status.running_version`), while the app header/footer stamp shows the
   full committed build version (`vA.B.<build> · <sha>`). The two surfaces
   disagreed, and the release-only string looked stale next to the footer.
2. The **Check now** button was disabled whenever the persisted upgrade-state
   row had no `git_remote`. But the row's `git_remote` is only populated *after*
   a discovery check runs (`check_latest_inline` → `_set_latest`/`_clear_latest`),
   so a freshly deployed control plane with `UPGRADE_GIT_REMOTE` set could not
   trigger its first check from this surface — a chicken-and-egg lockout.

## User-facing change
- **Current version** now renders the committed build version using the same
  build-time constants as the header/footer stamp:
  `v<release>.<buildNumber> · <commit>` (via the existing local
  `formatBuildVersion` helper and `__APP_VERSION__` / `__APP_BUILD_NUMBER__` /
  `__APP_COMMIT__`). The hint shows the release version (`Release vA.B.0`) for
  reference. This row no longer waits on the `/upgrade/status` poll to paint.
- **Check now** is enabled regardless of whether a git remote has been recorded
  (only disabled while a check is already in flight). The `/api/upgrade/check`
  call is throttled (429) and safe with no remote — it simply clears the latest
  fields and updates `latest_checked_at`.
- When a check runs and the refreshed status still has no `git_remote`, the
  result message reads "No upgrade remote is configured — nothing to check."
  instead of a misleading "Checked just now." The persistent "No upgrade remote
  is configured" info line is unchanged.

## API / IaC diff summary
- No backend or IaC change. Reuses the existing `upgradeApi.status()` /
  `upgradeApi.check()` typed clients and the build-time version constants.
- Frontend only — `web/src/components/SettingsPanel.tsx`:
  - `UpdatesSection` "Current version" row now shows the build/commit version.
  - "Check now" button `disabled` drops the `!configured` term.
  - `handleCheck` tailors the result message based on the refreshed
    `git_remote`.

## Validation evidence
- `cd web && npm run build` — succeeds.
- `cd web && npm test -- --run` — 70 files / 632 tests passing.
- No `*.test.*` targets the Settings panel directly; no fixture/mocks reference
  the changed row text.
