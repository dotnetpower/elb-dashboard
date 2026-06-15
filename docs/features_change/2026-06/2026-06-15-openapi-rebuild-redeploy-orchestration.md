---
title: One-action elb-openapi rebuild + redeploy from the dashboard
description: A single "Rebuild & Deploy" action builds the pinned elb-openapi image in ACR and redeploys it to AKS, enforcing the charter build-then-deploy rollout order.
tags:
  - infra
  - architecture
---

# One-action `elb-openapi` rebuild + redeploy

## Motivation

When the sibling `elastic-blast-azure` `docker-openapi` app changes (and the
`IMAGE_TAGS["elb-openapi"]` pin is bumped), bringing the change live previously
required two manual, separately-tracked dashboard actions in the right order:
build the image (ACR card) **then** deploy it (OpenAPI deploy panel). Doing them
out of order — bumping the pin / deploying before the image exists in ACR — is
the exact failure the charter's "build+push FIRST, then deploy" rollout order
exists to prevent (it manifests as an `ImagePullBackOff`).

This adds a single browser action that performs both steps in the safe order and
gates the deploy on a successful build, so a broken or missing image can never
replace the live revision.

## User-facing change

- The OpenAPI deploy panel gains a **"Rebuild & Deploy"** button next to Deploy.
  Clicking it:
  1. schedules an ACR build of `elb-openapi:<IMAGE_TAGS pin>` (reuses the same
     build context as the ACR card's "Build images"),
  2. polls the build until it reaches `Succeeded` (bounded; 30 min ceiling),
  3. **only on a succeeded build**, enqueues the existing
     `deploy_openapi_service` task and hands off to the normal deploy status
     tracking.
- A failed, timed-out, or unscheduled build surfaces an error and **never
  deploys** — the live revision is untouched.
- The button does not require the image to already be built (it builds it), so it
  works from a clean ACR.

## API / IaC diff summary

- New Celery task `api.tasks.openapi.rebuild_and_redeploy`
  ([api/tasks/openapi/rebuild.py](../../../api/tasks/openapi/rebuild.py)):
  build → bounded poll → deploy-on-success gate. The image tag is always the
  `IMAGE_TAGS["elb-openapi"]` pin (single source of truth — the task never
  invents a tag). `dry_run=true` performs no side effects (safe probe).
  Per-task soft/hard time limits sit above the poll ceiling so the worker is
  never SIGKILLed mid-poll. Reuses `api.tasks.acr._schedule_acr_build` for the
  build and `deploy_openapi_service` (by name) for the deploy.
- New routes in [api/routes/aks/openapi.py](../../../api/routes/aks/openapi.py):
  `POST /api/aks/openapi/rebuild-deploy` (enqueue) and
  `GET /api/aks/openapi/rebuild-deploy/{id}/status` (orchestrator envelope;
  carries `deploy_task_id` once the build succeeds).
- Frontend: `aksApi.rebuildDeployOpenApi` / `rebuildDeployOpenApiStatus` typed
  clients ([web/src/api/aks.ts](../../../web/src/api/aks.ts)); `useDeployTask`
  tracks the build then adopts the chained `deploy_task_id`; `DeployActions`
  renders the new button. No IaC change.

## Background: why no separate build-source patch was needed

The dashboard's ACR build path builds the sibling **GitHub master** context. A
prior `patch-openapi-build-context.py` step was once required to inject the
dashboard's runtime policy (core_nt sharding translation, node-local SSD config,
ETA overlay). That patch content is now **already committed in sibling master**
(`docker-openapi/patch_elastic_blast.py`, `app/eta.py`, and the `main.py`
policy hooks, all run by the committed `Dockerfile`), so a plain
`build_images(["elb-openapi"])` already produces the correct image. The
orchestrator therefore only needed to chain build → deploy; it did not need to
reproduce the patch.

## Validation evidence

- `uv run pytest -q api/tests` → 3750 passed, 3 skipped.
- New `api/tests/test_openapi_rebuild.py` (11 tests): deploy-only-on-success
  gate (build Failed / Timeout / schedule-failed never deploy), bounded poll
  timeout, `dry_run` no-side-effects, image tag is the pin, route 400 on missing
  params, route enqueue returns task id.
- `uv run ruff check` on all touched paths → clean.
- `cd web && npm run build` → built (tsc clean); `npx vitest run src/api/aks.test.ts` → 4 passed.
</content>
</invoke>
