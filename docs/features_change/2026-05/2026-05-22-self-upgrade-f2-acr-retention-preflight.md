# F2 — ACR retention pre-flight for rollback (2026-05-22)

## Motivation

PR3's rollback path issued the ARM PATCH unconditionally. If ACR
retention had purged any of the snapshotted tags between the upgrade
and the rollback attempt, ACA would accept the PATCH, attempt to pull
the missing image, and crashloop the new "rollback" revision —
delivering downtime instead of recovery. F2 adds a data-plane check
that catches this *before* the CAS so the operator gets a clean
refusal and can take the escape-hatch route.

## Change

* New dependency: `azure-containerregistry==1.2.0` for ACR data-plane
  manifest lookups (uses the same Managed Identity via
  `azure-identity`).
* `api/services/upgrade/acr_inventory.py` (new) — `lookup_images()`
  batches per-endpoint manifest probes and returns
  `ImageInfo(exists, created_on, error)` per ref. Never raises;
  distinguishes "tag not found" from "registry offline". Test seam:
  `set_client_factory_for_tests` injects a fake `ContainerRegistryClient`.
* `api/tasks/upgrade.py::start_rollback_inline` runs `lookup_images`
  for the three rollback target refs and raises
  `RollbackStartRefused("ACR no longer carries the snapshotted tags: …")`
  before the rollback CAS when any tag is missing. ACR-side errors
  (registry offline) are logged and the rollback proceeds — we'd
  rather attempt the PATCH than block on a transient SDK glitch.
* `api/routes/upgrade.py` — new admin-gated endpoint
  `GET /api/upgrade/rollback-preflight` returns per-image existence
  + creation timestamp so the SPA can warn proactively.
* `web/src/api/upgrade.ts` — adds `rollbackPreflight()` + types.
* `web/src/pages/UpgradePage.tsx` — renders the preflight result
  inside the Rollback card. When `available=false` a red banner lists
  the missing tags and the Roll-back button is disabled (escape-hatch
  card still works). When `available=true` a muted line confirms ACR
  pre-flight passed and shows the snapshot creation date.

## Tests

* `api/tests/test_upgrade_acr_inventory.py` (new) — parse, batch
  lookup, missing-tag flag, malformed-ref tolerance, `image_exists`
  shortcut.
* `api/tests/test_upgrade_task.py` — fixture seeds a default
  "always-exists" ACR stub; new
  `test_rollback_refuses_when_acr_tag_retention_purged` confirms the
  refusal path leaves state untouched.
* `api/tests/test_upgrade_routes.py` — fixture acr stub for routes
  fixture; new tests cover `/rollback-preflight` available/missing/
  no-snapshot/auth.

## Validation

* `uv run ruff check api/services/upgrade api/routes/upgrade.py api/tasks/upgrade.py api/tests/test_upgrade_*.py` — clean.
* `uv run pytest -q api/tests` — 1186 passed (vs prior 1176).
* SPA built (after clearing `web/.tsbuild` due to a pre-existing
  unrelated incremental cache issue affecting `cards/storage/*.tsx`).

## Known limitations

* `rollback_available_until` is still empty in the state row — the
  preflight endpoint surfaces `created_on` per image which is enough
  for the SPA to render a date. A retention-policy → "expires on"
  conversion is a follow-up that needs `azure-mgmt-containerregistry`
  policy reads.
* The preflight is a separate round-trip rather than being embedded
  in `/upgrade/status`. Keeps the status endpoint cheap; the SPA only
  hits preflight once per page render.
