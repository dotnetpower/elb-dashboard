# ACR image retention — keep newest 3, prune older on deploy/upgrade

## Motivation

Every control-plane deploy pushes a fresh image tag per sidecar repository
(`elb-api`, `elb-frontend`, `elb-terminal`). Both the in-app self-upgrade
(Settings → Update) and `scripts/dev/quick-deploy.sh` therefore accumulate one
manifest per run in the platform ACR forever, growing registry storage
unbounded. There was no automatic cleanup — old, unreferenced images piled up.

## User-facing change

Old images are now pruned automatically, keeping only the newest **3**
manifests per control-plane repository (configurable). The prune runs at the
two moments the user named:

* **In-app upgrade** — after an upgrade reaches `succeeded`, the reconciler
  runs a best-effort retention sweep. The now-running images and the rollback
  target are protected so a subsequent rollback is never starved of its
  snapshot. Tunable via `UPGRADE_ACR_KEEP_IMAGES` (default 3).
* **`quick-deploy.sh`** — after a successful build, the script deletes older
  manifests for the repositories it just deployed, **while the ACR firewall is
  still open** (steady-state ACR is `publicNetworkAccess: Disabled`, so the
  prune runs between `acr_ensure_build_access` and `acr_restore_build_access`).
  Tunable via `ELB_ACR_KEEP_IMAGES` (default 3); skip with `--no-prune` or
  `ELB_SKIP_ACR_PRUNE=1`. Skipped automatically on `--no-build` (no fresh image
  was pushed that run).

Both paths are **best-effort**: a missing `AcrDelete`/`Contributor` permission,
a transient registry error, or a repository with ≤ keep manifests is a no-op
and never fails the deploy/upgrade. The newest `keep` manifests (by
last-update time) are never deleted, so the just-pushed image and the
previously-running image always survive.

## API / IaC diff summary

* New service module `api/services/upgrade/acr_retention.py` —
  `prune_repository`, `prune_control_plane_images`, `keep_count`. Data-plane
  prune via `azure-containerregistry` (`list_manifest_properties` +
  `delete_manifest`); reuses `acr_inventory` for client construction + ref
  parsing.
* `api/tasks/upgrade/reconciler.py` — new `_prune_acr_after_success(row)` helper
  called on both the single-mode and blue/green `succeeded` transitions. Fully
  best-effort; never raises.
* `scripts/dev/quick-deploy.sh` — new `acr_prune_repo_keep_recent` /
  `acr_prune_targets` helpers, `--no-prune` flag, `ELB_ACR_KEEP_IMAGES` /
  `ELB_SKIP_ACR_PRUNE` env knobs; invoked before the final "Done" in the `all`
  and single-sidecar deploy paths.
* No Bicep / RBAC change. The shared user-assigned MI already holds
  `Contributor` on the registry (`infra/modules/acr.bicep`). **Caveat:** the
  data-plane `delete_manifest` content-delete action maps to the `AcrDelete`
  role; if a deployment's MI lacks it the in-app prune logs
  `forbidden (MI needs AcrDelete)` per repo and the upgrade still succeeds (the
  registry simply keeps accumulating until `AcrDelete` is granted as an
  additive phase-1 role assignment per charter §12a Rule 1).

## Validation evidence

* `uv run pytest -q api/tests/test_upgrade_acr_retention.py` → 13 passed
  (keep-newest-N, protected tag/digest outside window, env override, forbidden
  delete best-effort, list-not-found no-op, control-plane orchestration).
* `uv run pytest -q api/tests/test_upgrade_task.py api/tests/test_upgrade_chaos.py`
  → 62 passed (success hook does not break the state machine).
* `uv run pytest -q api/tests/test_upgrade_routes.py api/tests/test_upgrade_bluegreen.py api/tests/test_upgrade_acr_inventory.py`
  → 73 passed (blue/green `confirming → succeeded` prune hook safe).
* `uv run ruff check` clean on the changed Python files.
* `bash -n scripts/dev/quick-deploy.sh` → syntax OK.
