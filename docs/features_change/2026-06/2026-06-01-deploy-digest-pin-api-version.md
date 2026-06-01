# Deploy: pin images to digests + bake api version

## Motivation

A GitHub Actions deploy "succeeded" but the running version never changed.
Two independent bugs in `scripts/dev/quick-deploy.sh` were responsible.

### Bug A — mutable-tag PATCH is a silent no-op (user-visible)

`deploy.yml` runs `quick-deploy.sh all latest-main --no-build`, i.e. it patches
the Container App to the mutable tag `latest-main` without rebuilding. Azure
Container Apps only rolls a **new revision** when the template's image *string*
changes. When the active revision already references `…/elb-api:latest-main`,
patching it to the same `…/elb-api:latest-main` is a byte-for-byte no-op — the
freshly built image pushed under the same tag is silently ignored and the old
image keeps running.

Evidence: ACR `latest-main` was pushed at 08:09, but the active revision
`ca-elb-dashboard--0000053` was created at 04:39 and kept serving the stale
image. The SPA header version therefore never changed.

### Bug B — api image never carried the release version (secondary)

The api `az acr build` invocations passed **no** `APP_VERSION` /
`APP_GIT_COMMIT` / `APP_BUILD_TIME` build args (only the frontend build did),
so the `elb-api` image always baked the Dockerfile default and `/api/health`
reported `0.0.0+unknown`.

## User-facing change

* Re-running the deploy now reliably rolls a new revision so the running
  version matches the freshly built image.
* `/api/health` reports the real release version / commit / build time once the
  api image is rebuilt with the new build args.

## Change summary

`scripts/dev/quick-deploy.sh`:

* New `resolve_image_digest()` helper resolves a tag ref
  (`registry/image:tag`) to its immutable digest ref
  (`registry/image@sha256:…`) via `az acr manifest show-metadata`. Falls back
  to the tag ref (with a stderr warning) if the lookup fails, so a transient
  ACR read error degrades to the old behaviour rather than aborting the deploy.
* Both PATCH paths (`all` PATCH_PLAN and the single-sidecar `$NEW_IMAGE` loop)
  now pin the image to its digest before `az containerapp update`, guaranteeing
  a distinct template — and therefore a new revision — for every distinct
  build, even under a mutable tag.
* The api `az acr build` (both the `all` and single-sidecar paths) now passes
  `--build-arg APP_VERSION / APP_GIT_COMMIT / APP_BUILD_TIME`, matching the
  ARG names in `api/Dockerfile`.

No Bicep / IaC change. No api/web source change.

## Validation

* `bash -n scripts/dev/quick-deploy.sh` → SYNTAX OK.
* Verified `APP_VERSION_VAL` / `GIT_COMMIT_VAL` / `BUILD_TIME_VAL` are computed
  before every api build that now references them (all path lines 345-348;
  single-sidecar path computes them in the new api/worker/beat branch).
* Verified `resolve_image_digest` is defined before both PATCH paths and that
  `az acr manifest show-metadata <ref> --query digest -o tsv` returns the
  digest for `acrelbdashboard3abp67bppe.azurecr.io/elb-api:latest-main`.

Deploy tooling is validated by reading + dry checks per the charter; no
redeploy was triggered as part of this change.
