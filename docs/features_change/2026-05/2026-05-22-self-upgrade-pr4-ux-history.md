# PR4 — self-upgrade UX + history (2026-05-22)

## Motivation

PR1–3 made the upgrade flow end-to-end functional through the API.
PR4 (this) adds the operator-visible surface: a header badge that
appears when a new release is available, a `/upgrade` page that drives
the start / progress / rollback / escape-hatch interactions, and a
persistent audit history so a maintainer can investigate what
happened after the producing revision has been torn down.

## User-facing change

### Frontend

* New header badge (`UpgradeBadge`) next to the existing
  `v<release>` stamp. Visible only when the persisted state row signals
  either a newer release is available or an upgrade is in flight /
  failed. Clicking the badge routes to `/upgrade`.
* New `/upgrade` page surfaces:
  * Current vs latest version, state, progress, last-check timestamp.
  * Candidate tag dropdown + "I accept ~1 min downtime" checkbox + Start.
  * Diff table between `current_images` and `rollback_target` plus a
    Rollback button (only when a snapshot is recorded).
  * Copyable escape-hatch command set (admin only — others get a soft
    "not on the allowlist" hint).
  * Tail of the audit history (newest first, last 20 events).
* All actions go through the typed `web/src/api/upgrade.ts` client; no
  raw `fetch` in the page.

### Backend

* `GET /api/upgrade/history?limit=N` — returns the tail of the
  upgrade-history Append Blob. Auth: any signed-in caller.
* `api/services/upgrade/history.py` — append-blob writer/reader. The
  writer is best-effort: any backend failure is swallowed so audit
  logging never breaks an upgrade.
* The task transitions (`start`, `escape_hatch`, `succeeded`,
  `failed_pre`, `failed_rollout`, `rollback_start`, `rollback_done`)
  now each emit a history event so the SPA page has live evidence to
  render.

## Backend changes

* `api/services/upgrade/history.py` (new) — Append Blob writer + reader
  with an in-memory backend for tests. Refuses to construct the
  in-memory backend outside `PYTEST_CURRENT_TEST` unless
  `ELB_ALLOW_INMEMORY_UPGRADE_HISTORY=true` is explicitly set.
* `api/tasks/upgrade.py` — wires `history.record_event` calls into
  every major transition. Uses `record_event` (never raises) so audit
  failures can't break the pipeline.
* `api/routes/upgrade.py` — adds `GET /upgrade/history`, plumbed
  through `require_caller`.

## Frontend changes

* `web/src/api/upgrade.ts` (new) — typed client mirroring every
  upgrade endpoint, plus `compareSemver`, `isUpgradeAvailable`, and
  `statePhase` helpers used by the badge and page.
* `web/src/components/UpgradeBadge.tsx` (new) — polls `/upgrade/status`
  every 60 s; renders a colour-coded pill (info / warn / danger / ok)
  with a router link to `/upgrade`. Renders nothing while the row is
  `idle` AND no newer version is published, so the chrome stays clean
  in fresh deployments.
* `web/src/pages/UpgradePage.tsx` (new) — the operator console for the
  flow.
* `web/src/App.tsx` — registers `<Route path="/upgrade" element={<UpgradePage />} />`.
* `web/src/components/Layout.tsx` — imports `UpgradeBadge` and places
  it inside `layout__logo-sub` next to the version stamp.

## Test changes

* `api/tests/test_upgrade_history.py` (new) — round-trip, ordering,
  tail cap, corrupt-line tolerance, and the never-raise invariant on
  backend failure.
* `api/tests/test_upgrade_task.py` — task fixture now also seeds the
  in-memory history backend.
* `api/tests/test_upgrade_routes.py` — fixture seeds history backend;
  added `/upgrade/history` happy-path + auth tests.
* SPA: no unit tests added (the page is a thin renderer over the typed
  client which is itself covered by the backend route tests). Build
  passes `npm run build` (tsc strict + vite) and `npx eslint`
  on the three new files.

## Validation

* `uv run ruff check api/services/upgrade api/routes/upgrade.py api/tasks/upgrade.py api/tests/test_upgrade_*.py` — clean.
* `uv run pytest -q api/tests` — 1172 passed (no regression vs prior 1165).
* `cd web && npm run build` — succeeds with the existing warnings only
  (large chunk warning unchanged from main).
* `cd web && npx eslint src/api/upgrade.ts src/components/UpgradeBadge.tsx src/pages/UpgradePage.tsx --max-warnings 0` — clean.

## IaC / infra

No Bicep changes. The append-blob container `upgrade-history` is
created on first write (same pattern as the build-log container in
PR2).

## Operator setup

No new env variables. Required envs from earlier PRs still apply:

| Env | Purpose | Introduced |
|---|---|---|
| `UPGRADE_GIT_REMOTE` | URL of the operator's git remote | PR1 |
| `PLATFORM_ACR_NAME` | ACR name without `.azurecr.io` | PR2 |
| `UPGRADE_ADMIN_OIDS` | comma-separated admin oids | PR2 |
| `AZURE_BLOB_ENDPOINT` | platform Storage blob endpoint | existing |
| `AZURE_TABLE_ENDPOINT` | platform Storage table endpoint | existing |
| `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`, `CONTAINER_APP_NAME` | from azd | existing |

## Known limitations (deferred)

* **ACR retention pre-flight.** The rollback button still does not
  pre-verify that the snapshotted tags exist in ACR; the SPA does not
  yet render a retention countdown. The plumbing (`rollback_available_until`
  field) is in place but unpopulated until a follow-up adds the
  data-plane probe.
* **Live build-log streaming.** The backend exposes the per-component
  build log blob, but the page does not yet stream it inline; an
  operator follows the link manually. Streaming view is a follow-up.
* **App Registration role.** The `UpgradeAdmin` decision is still
  env-allowlist based. Switching to an MSAL `roles` claim only needs
  the App Registration change; the code path already prefers the
  claim when present.
* **Major-version (`A`) extra confirmation.** Not yet rendered in the
  modal; the design doc has it scheduled for a follow-up.
* **Unrelated lint hygiene.** `web/.tsbuild/` (vite's internal config
  cache) is not in `eslint.config.js` `ignores`; running the full
  `npm run lint` reports two pre-existing `no-unused-vars` errors
  against `.tsbuild/node/vite.config.js`. The PR4 files lint clean on
  their own; the `.tsbuild` ignore fix is out of scope for this PR.
