# elb-openapi promoted to core image + per-image Build buttons

**Date**: 2026-05-12
**Scope**: `api/services/image_tags.py`, `api/function_app.py`,
`web/src/api/endpoints.ts`, `web/src/components/cards/AcrCard.tsx`

## Motivation

`elb-openapi` is required by the API Reference page (it serves the
live OpenAPI spec from the AKS cluster) and by every workspace that
wants discoverable BLAST endpoints. Treating it as "(optional)" in
the ACR card created a UX trap: the AKS provision orchestrator
expected the image to exist but the ACR card hid it from the
operator's mental model. Several test workspaces ended up without
the image, surfacing as "OpenAPI service not found" downstream.

Operators also asked for a way to rebuild a single image (e.g. only
`elb-openapi` after a code change) without re-running every build.

## User-facing change

- The ACR card now lists `elb-openapi` in the same "core" set as
  `ncbi/elb`, `ncbi/elasticblast-job-submit`, and
  `ncbi/elasticblast-query-split`. The "(optional)" tag is gone.
- Each row in the ACR image table now has a per-image **Build**
  button when the image is missing. Clicking it triggers a single
  ACR Build Task and updates only that row.
- The card-level "Build All" action and the per-image action share
  the same elapsed-timer state, so concurrent builds report a
  consistent live status.
- The existing build progress banner shows the in-flight image name
  (e.g. `Building elb-openapi… 0m 42s`).

## API / IaC diff summary

`api/services/image_tags.py`:
- `IMAGE_BUILD_INFO["ncbi/elasticblast-job-submit"].pre_build_cmd`
  changed from `rsync -a src/elastic_blast/templates docker-job-submit/`
  to `cp -r src/elastic_blast/templates docker-job-submit/`.
  ACR Build Tasks images do not ship `rsync`; `cp -r` works in the
  default ACR build environment.
- `IMAGE_TAGS["elb-openapi"]` was already declared, no change.

`api/function_app.py` `build_acr_images`:
- Accepts an optional `images: string[]` field in the JSON body. When
  set, the route iterates `IMAGE_TAGS` and skips images not in the
  request list. Empty / missing list preserves the existing
  build-all behaviour.

`web/src/api/endpoints.ts`:
- `monitoringApi.buildAcrImages` gains a fourth parameter
  `images?: string[]` and threads it into the POST body.
- The result type extends with `acr_status?: string` so live ACR run
  state can be displayed per row.

`web/src/components/cards/AcrCard.tsx`:
- `CORE_IMAGES` Set extended with `elb-openapi`.
- New `singleBuilding` state tracks the image targeted by a per-row
  Build click. The build timer is hoisted into a separate effect so
  it survives across single-image and build-all flows.
- Server-side detection: when `hasServerBuilding` flips on after a
  page refresh, the card adopts the "building" state instead of
  showing the stale "Build in progress (started externally)" hint.
- New per-row state cell renders a Build button when the image is
  missing, a "Starting" pill while the request is in flight, and the
  existing live ACR status pill once the run is queued.

## Validation evidence

- `pytest -q api/tests/` → 13 passed.
- `npx tsc --noEmit` (web) → clean.
- `npx vite build --mode production` → succeeded.
- Function App + SPA already redeployed with these changes via
  `WEBSITE_RUN_FROM_PACKAGE` and `azd deploy web --no-prompt`.
- Pending: user verifies the per-image Build button on the deployed
  Dashboard.
