# Quick deploy live manifest protection

## Motivation

An `all` deployment with tag `20260916073535` exposed a retention race in
`scripts/dev/quick-deploy.sh`. The
[Azure Container Registry (ACR)](https://learn.microsoft.com/azure/container-registry/container-registry-intro)
sweep kept the newest three manifests,
but build-only and out-of-band builds had already moved the running revision's
digests outside that window. The sweep deleted those live digests before the
script began its sequential sidecar patches. The first intermediate revision
therefore failed with `MANIFEST_UNKNOWN` for the unchanged sidecars.

The previous healthy
[Azure Container Apps revision](https://learn.microsoft.com/azure/container-apps/revisions)
remained available throughout the failed rollout. The deployment was recovered
with one [Azure Resource Manager (ARM)](https://learn.microsoft.com/azure/azure-resource-manager/management/overview)
template PATCH that moved all runtime sidecars to the newly built digests,
followed by a second atomic PATCH that converged worker, beat, and terminal
environment and resource settings.

## User-facing change

Quick deploy retention now protects every image digest referenced by the
current Container App template or any active revision, even when that digest is
older than the configured keep window. Tag-based live references are resolved
to immutable digests before deletion decisions are made. The image built for
the current deploy tag is protected explicitly rather than relying on it to
remain inside the newest-N window. Live references are read again after
manifest listing and immediately before deletion begins, so a concurrent
rollout that starts using an older digest is protected without an ARM request
for every deletion candidate.

If the script cannot read live image references or resolve a live tag, pruning
fails closed and keeps the extra manifests. Image deployment can continue;
retention never risks making a revision unstartable.

## API / IaC diff summary

* `scripts/dev/quick-deploy.sh` reads current-template and active-revision image
  references before pruning and passes protected digests into each repository
  sweep.
* `api/tests/test_quick_deploy_acr_prune.py` covers digest references, tag
  resolution, stale-manifest deletion, and fail-closed lookup behavior with a
  fake Azure CLI.
* No API contract, Bicep resource, RBAC assignment, or dependency changed.

## Validation evidence

* Deployment tag `20260916073535`; final revision
  `ca-elb-dashboard--converge-1789545321` is Healthy with 100% traffic.
* All six sidecars are Ready and Running with zero restarts.
* `https://dashboard.elasticblast.com/api/health` returned HTTP 200 with
  version `0.3.0` and the final revision name.
* ACR returned to `publicNetworkAccess=Disabled` and `defaultAction=Deny`.
* `uv run pytest -q api/tests/test_quick_deploy_acr_prune.py` -> 5 passed.
* `uv run pytest -q api/tests -m ''` -> 6104 passed, 4 skipped.
* `bash -n scripts/dev/quick-deploy.sh` -> syntax OK.
* `uv run python scripts/dev/check_mypy_baseline.py` -> baseline unchanged.