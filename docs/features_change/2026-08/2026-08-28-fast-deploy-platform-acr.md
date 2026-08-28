---
title: Fast-deploy platform ACR convergence
description: Backfill the platform registry coordinate during fast sidecar patches so self-upgrade and revision maintenance cannot inherit an older incomplete template.
tags: [operate, release]
---

# Fast-deploy platform ACR convergence

## Motivation

A live [Azure Container Apps](https://learn.microsoft.com/azure/container-apps/overview) revision predated the Bicep wiring for `PLATFORM_ACR_NAME`. Its API, worker, and beat containers therefore had no platform registry coordinate. An in-app upgrade safely stopped before changing traffic with `PLATFORM_ACR_NAME is not set; cannot run az acr build`, but another image-only fast deploy would have preserved the same drift.

## User-facing change

Future fast deployments repair the platform registry coordinate while patching runtime sidecars. Self-upgrade image builds and revision tag maintenance no longer depend on a full provision having refreshed an older live template first.

## API and infrastructure summary

- `quick-deploy.sh` upserts `PLATFORM_ACR_NAME` from its already validated `ACR_NAME` target on API, worker, beat, and terminal patches.
- The sidecar set matches the existing Bicep declaration and excludes the frontend container.
- The value is a non-secret [Azure Container Registry](https://learn.microsoft.com/azure/container-registry/container-registry-intro) resource name. No role assignment, network rule, Storage setting, secret, or public endpoint changed.
- A missing control-plane environment source still preserves the existing warning and image-only fallback instead of masking missing guard configuration.

## Validation

- Focused control-plane environment tests: 23 passed.
- `bash -n scripts/dev/quick-deploy.sh`: passed.
- Live failure was reproduced before repair; the worker-only environment upsert then allowed the same upgrade target to advance from `failed_pre` to the ACR build pipeline.
- Full backend and documentation validation is recorded with the remediation commit.
