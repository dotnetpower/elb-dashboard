---
title: Fast-deploy platform ACR convergence
description: Backfill the platform registry coordinate during fast sidecar patches so self-upgrade and revision maintenance cannot inherit an older incomplete template.
tags: [operate, release]
---

# Fast-deploy platform ACR convergence

## Motivation

A live [Azure Container Apps](https://learn.microsoft.com/azure/container-apps/overview) revision predated the Bicep wiring for `PLATFORM_ACR_NAME`. Its API, worker, and beat containers therefore had no platform registry coordinate. An in-app upgrade safely stopped before changing traffic with `PLATFORM_ACR_NAME is not set; cannot run az acr build`, but another image-only fast deploy would have preserved the same drift. Live repair also proved that Azure CLI could report success for `--container-name beat --set-env-vars ...` while leaving the beat environment unchanged.

## User-facing change

Future fast deployments repair the platform registry coordinate while patching runtime sidecars. Self-upgrade image builds and revision tag maintenance no longer depend on a full provision having refreshed an older live template first.

## API and infrastructure summary

- `quick-deploy.sh` upserts `PLATFORM_ACR_NAME` from its already validated `ACR_NAME` target on API, worker, beat, and terminal patches.
- Runtime environment changes use a fresh full-resource snapshot, an exact-container JSON mutator, and a template-only ARM REST patch. The script compares the resource ETag immediately before submission, sends `If-Match` as defense in depth, and re-reads the applied values after revision readiness. Image/resource updates no longer pass Azure CLI env flags that can target the wrong container.
- Image, resources, and environment are submitted in one sidecar revision. The helper is idempotent, waits at most five minutes for `Provisioned` and `Running`, verifies the full desired container state, and refuses success if another revision became latest.
- The validated stable Container Apps API version defaults to `2026-01-01` and can be overridden with `CONTAINER_APP_API_VERSION` for a controlled platform migration.
- Missing guard-policy source or a failed M2M secret upsert now aborts the deployment instead of continuing with stale or invalid runtime configuration.
- Mutable image tags must resolve to an immutable digest; three bounded lookup failures stop the deploy rather than silently reusing a tag that may not roll a revision.
- The sidecar set matches the existing Bicep declaration and excludes the frontend container.
- The value is a non-secret [Azure Container Registry](https://learn.microsoft.com/azure/container-registry/container-registry-intro) resource name. No role assignment, network rule, Storage setting, secret, or public endpoint changed.
- The control-plane environment source is required; a missing or malformed file stops the deploy before any sidecar patch.
- Persona impact: none. Existing route authorization and Reader/Contributor/Owner capabilities are unchanged.

## Validation

- Focused deploy-helper suite: 37 passed, covering sidecar ownership, image/resources/env atomicity, plain and secret references, no-op behavior, malformed input, REST/ETag wiring, active-revision verification, bounded polling, and immutable digest retries.
- `bash -n scripts/dev/quick-deploy.sh`: passed.
- Full backend validation including slow/subprocess markers: 5,601 passed and 4 environment-only parity tests skipped.
- Persona Matrix: 53 passed.
- Live failure was reproduced before repair; the worker-only environment upsert then allowed the same upgrade target to advance from `failed_pre` to the ACR build pipeline.
- Live template verification on revision `ca-elb-dashboard--acr-rest-1787923746` confirmed API, worker, beat, and terminal all carried `PLATFORM_ACR_NAME=acrelbdashboardcyutlgcnv3`; liveness and readiness remained green.
- Full backend and documentation validation is recorded with the remediation commit.
