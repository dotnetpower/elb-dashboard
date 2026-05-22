# Self-upgrade — 40-point critique hardening (2026-05-22)

## Motivation

Widened the critique surface from 20 to 40 items covering auth, state
machine, ARM/ACR, git/build, storage, SPA UX, ops/docs, and tests.
Picked up four High/Medium items that the 20-point pass had missed
plus the obvious doc + storage-retention gaps.

## Findings + fixes

### High → Hardened

**#6 — `enqueue_failed` exception object on `phase_detail` could leak
broker credentials (redis URL with embedded password).**
`api/tasks/upgrade.py::start_upgrade_inline` now records only the
exception class name and runs it through
`api.services.sanitise.sanitise`. Full traceback still goes to the
api log via `LOGGER.exception`.

### Medium → Hardened

**#3 — UpgradeAdmin role match was case-sensitive.**
`api/services/upgrade/auth.py::is_upgrade_admin` casefolds both sides
so `upgradeadmin`, `UPGRADEADMIN`, … all resolve. The oid allowlist
stays case-sensitive because AAD canonicalises GUIDs.

**#5 + #26 + #27 — SPA spammed the browser console with 403s when a
non-admin opened `/upgrade`.**
`web/src/pages/UpgradePage.tsx::refreshAll` now probes the escape-hatch
endpoint first; if it 403s the page sets `adminBlocked=true` and
short-circuits the rollback-preflight request. The build-log viewer
now also skips polling when `jobId` is empty
(`web/src/components/BuildLogViewer.tsx`).

**#10 — `rolling_out` could be locked for up to 15 minutes when the
producing worker crashed between the state commit and `begin_update`.**
`api/tasks/upgrade.py::reconcile_rolling_out_inline` adds a 2-minute
fast-fail (`PATCH_NEVER_LANDED_GRACE_SECONDS`): if the row has been
`rolling_out` longer than that AND the live ACA template still carries
the old api image ref, the row moves straight to `failed_rollout`. The
old 15-minute stuck guard remains as the upper bound.

**#24 — `upgrade-logs` / `upgrade-history` containers have no
retention policy.**
`docs/user-guide/upgrades.md` now ships a copy-pasteable
`az storage account management-policy create` example that targets
both prefixes with a 180-day expiry.

**#32 — `docs/troubleshooting.md` had no self-upgrade section.**
Added five concrete scenarios: header badge never appears, Start 403,
Start 409, rolling_out past budget, "ACR no longer carries the
snapshotted tags", and empty build log.

**#36 — `test_version.py` reload could pollute downstream tests with
a stray `APP_VERSION` env or a stale `api.__version__`.**
Added an `autouse` fixture that pops `APP_VERSION` and reloads `api`
after every test in the file.

### Low — Logged, no code change

The remaining 32 items are bounded by Azure-side guarantees, by the
existing throttles, or are explicit operational tradeoffs (e.g. the
`/tmp/elb-upgrade` accumulation that the next sidecar restart wipes).
Test gaps (UpgradePage / BuildLogViewer unit tests, integration test)
are noted for a follow-up — the public route + service unit tests
already cover the contract.

## Tests

No new test files; the existing suite continues to exercise the
changed code paths.

## Validation

* `uv run ruff check api/services/upgrade api/routes/upgrade.py api/tasks/upgrade.py api/tests/test_upgrade_*.py api/tests/test_version.py` — clean.
* `uv run pytest -q api/tests` — 1201 passed.
* `cd web && npm run build && npm run lint` — clean.
