---
title: Recover stale-open ACR build access
description: Return an idle deployment registry to private steady state when a previous interrupted build left public Allow access enabled.
tags:
  - security
  - infra
  - operate
---

# Recover stale-open ACR build access

## Motivation

Post-deployment validation found the platform
[Azure Container Registry](https://learn.microsoft.com/azure/container-registry/container-registry-intro)
in `publicNetworkAccess=Enabled`, `defaultAction=Allow` even though the Bicep
steady state is private-only. The shared ACR build helper restored a registry
only when the current process had opened it. If a killed or interrupted earlier
deploy left build access open, every later deploy classified that state as the
original policy and preserved it indefinitely.

## User-facing change

- A deploy that starts while ACR build access is already open now marks the
  registry for private steady-state recovery after its builds complete.
- Recovery happens only when an active-task query proves zero other ACR builds
  are running. A query failure or non-zero count fails safe by leaving access
  open and emitting an explicit warning rather than interrupting another build.
- Worker-side stale-open healing also requires an approved ACR private endpoint;
  without a safe private pull path, Rebuild & Deploy fails before scheduling a
  build instead of closing access and breaking the running control plane.
- Existing IP allowlist rules are captured and restored byte-for-byte on the
  ordinary temporary-open path.
- Operators intentionally maintaining public build access can set
  `ACR_BUILD_ACCESS_PRESERVE_OPEN=1` for that invocation.
- The normal private-origin path is unchanged: open temporarily, wait for policy
  propagation, build, then restore the captured private policy.
- The browser-triggered OpenAPI **Rebuild & Deploy** task now uses the same
  lease semantics through the Azure SDK. It bounds the image build to 20
  minutes, restores ACR before enqueueing the AKS deployment, and returns a
  terminal `acr_build_access_restore_failed` result rather than deploying while
  the registry remains open. Dry-run performs no network mutation.
- If the build poll itself reaches its deadline, the task requests cancellation
  and waits up to three minutes for a terminal ACR state before restoration. It
  never excludes its own still-active run from the active-build guard.

## API / IaC diff summary

- `scripts/dev/acr-build-access.sh` adds an idle-checked stale-open recovery path.
- `api/services/acr_build_access.py` provides the worker-side bounded lease;
  `api/tasks/openapi/rebuild.py` enforces restore-before-deploy.
- No role, Container App, private endpoint, or Bicep resource is changed.
- The live platform ACR had an approved private endpoint and was remediated to
  `publicNetworkAccess=Disabled`, `defaultAction=Deny`, zero IP rules, with
  trusted Azure services retained.

## Validation

- `bash -n scripts/dev/acr-build-access.sh`
- `shellcheck scripts/dev/acr-build-access.sh`
- `uv run pytest -q api/tests/test_acr_build_access.py -m subprocess`
  covers idle recovery, concurrent-build preservation, active-query failure,
  explicit preserve, and the ordinary temporary-open/restore path.
- `uv run pytest -q api/tests/test_acr_build_access_service.py
  api/tests/test_openapi_rebuild.py` covers SDK open/restore, propagation
  bounds, active/unknown build deferral, dry-run, and deploy gating.
- The deployed Container App remained Healthy with all six containers Ready and
  zero restarts after the live posture remediation.
- Final live state: ACR `publicNetworkAccess=Disabled`, `defaultAction=Deny`,
  trusted services retained, zero IP rules; Storage remains
  `publicNetworkAccess=Disabled`, `defaultAction=Deny`, zero IP rules.
- Patched OpenAPI image build `de7s` succeeded as immutable tag `4.38` / digest
  `sha256:73073111e32ae7fc03b4d2f23e6ecbe7ef0247ade0a67b52b4087b412e01f85c`,
  and ACR was verified private again after the build.
