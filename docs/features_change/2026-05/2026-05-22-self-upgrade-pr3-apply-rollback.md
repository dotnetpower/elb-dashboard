# PR3 — self-upgrade apply + rollback + escape hatch (2026-05-22)

## Motivation

PR1 added discovery, PR2 added build. PR3 wires the actual ARM PATCH
that swaps the deployed Container App template to the freshly built
images, the rollout watcher / reconciler that finalises the state on
the post-restart revision, the rollback API, and the escape-hatch
command set the operator runs when the new revision fails to boot.

After PR3, the in-app self-upgrade flow is end-to-end functional from
the API surface. UI polish (badge, modal, retention countdown,
progress streaming) follows in PR4.

## User-facing change

Two new mutating endpoints (both `UpgradeAdmin`-gated):

* `POST /api/upgrade/rollback` — replays the snapshot taken before the
  upgrade (`rollback_target_json` on the row) by issuing a fresh ARM
  PATCH targeting the previous image refs. Allowed from `rolling_out`,
  `succeeded`, and `failed_rollout`. Returns the updated state row.
* `GET /api/upgrade/escape-hatch` — returns a copy-pasteable
  `az containerapp update` command set (one per container) that the
  operator can run from any `az login`-ed shell to restore the
  snapshot. Used when the new revision is unreachable and even the
  rollback API is dead.

State machine grows the post-PATCH portion:

```
... -> building -> patching -> rolling_out --> succeeded
                                          \-> failed_rollout
                                          \-> rolling_back -> rolled_back
                                                          \-> rollback_failed
```

`reconcile_rolling_out` (beat, 60 s) finalises `rolling_out` to
`succeeded` when the api's own `__version__` matches `target_version`
(i.e. we're the new revision), or to `failed_rollout` when the latest
revision is in a terminal failure state or has been stuck longer than
the 15-minute rollout budget.

## Backend changes

* `pyproject.toml` — adds `azure-mgmt-appcontainers==3.1.0`.
* `api/services/upgrade/aca_template.py` (new) — reads
  `Microsoft.App/containerApps` template, extracts per-role image refs
  (`api`/`frontend`/`terminal`; the `api` role is shared by api/worker/
  beat containers), and exposes `swap_images()` (PATCH for upgrade)
  and `apply_images()` (PATCH for rollback). `compute_target_images()`
  is the public helper used by tasks to turn a semver into the
  `{api,frontend,terminal}` image-ref tuple.
* `api/services/upgrade/rollout_watcher.py` (new) — polling helper
  that returns when a named revision reports `running=Running` +
  `provisioning=Provisioned`, or raises `RevisionUnhealthy` /
  `RevisionTimeout`. Reserved for PR4 streaming progress; PR3 only
  uses the cheaper `revision_status()` snapshot from the reconciler.
* `api/services/upgrade/escape_hatch.py` (new) — pure string builder.
  Emits one `az containerapp update --subscription … --name … --resource-group … --container-name … --image …`
  line per container (`api`, `worker`, `beat`, `frontend`, `terminal`).
  Each command carries `--subscription` explicitly so the operator's
  default profile is never mutated; no `az account set` is issued. No
  secrets are baked in.
* `api/tasks/upgrade.py` — extends `execute_upgrade_inline` with the
  post-build pipeline:
  1. `building → patching` (CAS): record snapshot of current images
     and target images; log the escape-hatch command set;
  2. `patching → rolling_out` (CAS): commit BEFORE the ARM PATCH so a
     producing-revision death is recoverable;
  3. `aca.swap_images(target_version, revision_suffix=…)` — the PATCH;
     on failure jumps to `failed_rollout`.
  Adds `start_rollback_inline`, `reconcile_rolling_out_inline`, and the
  Celery task wrappers. The reconciler has a 15-minute stuck guard so
  a perpetually-rolling_out row never blocks future starts.
* `api/routes/upgrade.py` — registers `/rollback` and `/escape-hatch`
  endpoints; both gated by `require_upgrade_admin`.
* `api/celery_app.py` — adds `upgrade-reconcile-rolling-out` beat
  entry (60 s).

## Test changes

* `api/tests/test_upgrade_aca_template.py` (new) — fake ARM resources +
  client, asserts image extraction, swap_images mutating api/worker/
  beat together while leaving sidecars like `redis` untouched, and
  apply_images on the rollback path.
* `api/tests/test_upgrade_rollout_watcher.py` (new) — drives the
  polling loop with injected `now`/`sleep`/client; covers healthy,
  transient unhealthy, terminal failure, and timeout.
* `api/tests/test_upgrade_escape_hatch.py` (new) — command shape,
  subscription/rg/app coverage, secret hygiene, env-missing
  placeholder fallback, and the no-`az-account-set` invariant.
* `api/tests/test_upgrade_task.py` — extended to the full state
  machine (queued → ... → rolling_out → succeeded via reconciler) and
  rollback round-trip; new tests for `failed_rollout` on PATCH refusal
  and rollback-without-snapshot 409.
* `api/tests/test_upgrade_routes.py` — `/rollback` admin gate,
  rollback no-snapshot 409, rollback happy path, `/escape-hatch`
  happy path + no-snapshot 404.

## Validation

* `uv run ruff check api/services/upgrade api/routes/upgrade.py api/tasks/upgrade.py api/tests/test_upgrade_*.py` — clean.
* `uv run pytest -q api/tests` — 1165 passed (no regression vs prior 1143).
* End-to-end against a real ACA still requires manual operator
  validation (the SDK round-trip cannot be unit-tested cheaply); the
  unit tests run the full task with a fake `aca` surface.

## IaC / infra

No Bicep changes. The user-assigned MI already has `Contributor` on
the workspace RG, which subsumes `Microsoft.App/containerApps/write`.

## Operator setup (additional from PR2)

No new env variables. The existing PR2 set (`UPGRADE_GIT_REMOTE`,
`PLATFORM_ACR_NAME`, `UPGRADE_ADMIN_OIDS`) plus the existing
`AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`, `CONTAINER_APP_NAME`
(set by `azd up`) are all PR3 needs.

## Known limitations (tracked for PR4)

* The rollback path does not yet **verify** the snapshotted image tags
  are still resolvable in ACR before issuing the PATCH. If ACR
  retention expired the rollback PATCH succeeds but ACA fails to pull
  and the new (rollback) revision crashloops. PR4 ships the
  retention-countdown UX and adds an ACR data-plane check ahead of
  rollback.
* The `started_by_oid` is overwritten on rollback so the row only
  carries the most recent actor. A separate `rollback_by_oid` field
  will follow with the PR4 history page.

## Out of scope (PR4)

* SPA UX (badge, modal, progress streaming, rollback diff, retention
  countdown).
* History page tailing the upgrade audit blob.
* Major-version (`A`) extra confirmation.
* ACR retention pre-flight for `/rollback`.
