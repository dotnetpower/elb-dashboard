---
title: OpenAPI derives a missing precise search space
description: Restore active-generation search-space derivation for direct precise core_nt API examples instead of rejecting them before metadata resolution.
tags:
  - blast
  - user-guide
---

# OpenAPI derives a missing precise search space

## Motivation

The curated `POST /v1/jobs` `core_nt` examples intentionally omit a snapshot-specific
`-searchsp` value because the active database generation owns that value. A stale validation guard
ran before the active-generation resolver and returned HTTP 400 with
`Precise core_nt sharding requires db_effective_search_space`, making those examples fail even
though the runtime already had the metadata and fallback logic needed to calculate the value.

## User-facing change

- Direct precise `core_nt` submissions may omit `-searchsp` and `-dbsize` again.
- All three curated `core_nt` API Reference presets now state that search space is resolved from
  the active generation and that callers should leave both raw options unset.
- The runtime reads the active generation's sequence and letter counts and derives one positive
  search space before dispatch.
- Caller-provided query-specific search spaces remain preserved.
- Missing, malformed, or inaccessible active-generation metadata still fails closed with HTTP 503.
- Until the OpenAPI runtime image is rebuilt, the deployed `2026-08-19` generation can be called
  by appending `-searchsp 30807003700117` to `blast_options.extra` on `POST /v1/jobs`.

## API and implementation summary

- The OpenAPI build-context patcher removes the obsolete required-search-space guard from both
  newly generated and previously patched source.
- The pinned runtime advances from `elb-openapi:4.50` to `elb-openapi:4.51`, preserving `4.50` as
  the rollback boundary.
- The ACR pre-build command now runs in a pinned tool image containing git and Python. Two live
  attempts correctly failed before deployment: the first exposed that ACR's implicit command image
  had no `git`, and the second proved that `image:` is not a supported cmd-step property. The final
  task uses the documented `cmd: <image> <command>` form. Both failures preserved the running
  `4.50` deployment and restored ACR to `publicNetworkAccess=Disabled`, `defaultAction=Deny`.
- Existing active-generation validation, immutable database paths, DB-order oracle checks, and
  Web BLAST statistical-context handling are unchanged.
- API Reference examples remain generation-neutral and do not embed a database snapshot constant.
- No authentication, RBAC, network, Storage, Kubernetes state, or infrastructure contract changed.

## Rollout reason

Tier 1 tests and a fresh pinned-sibling generation/compile smoke reproduce and verify the code
path, but the public `/v1/jobs` endpoint runs inside the AKS `elb-openapi` image and the curated
descriptions run inside the Container App `frontend` image. A runtime image rebuild plus frontend
deployment is therefore required for the user-requested live verification; no infrastructure or
sidecar topology is being reprovisioned.

## Validation

- `uv run pytest -q api/tests/test_openapi_rebuild.py api/tests/test_acr_build_task.py api/tests/test_patch_openapi_build_context.py scripts/dev/openapi-overlays/test_exact_oracle.py` - 120 passed.
- `npm --prefix web test -- src/pages/apiReference/spec.test.ts` - 5 passed.
- `uv run python scripts/docs/check_frontmatter.py` - passed.
- `uv run pytest -q api/tests` - 5,709 passed, 5 skipped.
- `uv run ruff check api scripts/dev/patch-openapi-build-context.py scripts/dev/openapi-overlays` - passed.
- `npm --prefix web test` - 999 passed; `npm --prefix web run build` - passed.
- `DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict` - passed.
- Fresh sibling commit `352a1f4ccf32dc8d76add5bcdb901530f0ad4c14` patch smoke - generated
  `main.py` and `exact_oracle.py` compiled, the obsolete HTTP 400 guard was absent, and the active
  database read plus fallback calls were present.
- Git commits `b199973f`, `0657c34c`, and `a0980a75` were pushed to `origin/main`; every push ran
  the isolated clean-checkout hook (`5,709 passed, 5 skipped`, Ruff, frontmatter, and strict docs).
- Container App rollout: API, worker, and beat converged on digest
  `sha256:c2a05f4abfe1e702766258507f68482bd4db9a5b5645311071435493c59dcf3e`; frontend
  converged on `sha256:43f3a3b284174a66951bbdba7aceb18a75a8e082087159ff634a204d2de3de04`.
- Browser validation loaded frontend build `v0.3.73` / `b199973f`. All three curated `core_nt`
  presets displayed the active-generation search-space guidance, and their request bodies contained
  neither `-searchsp` nor `-dbsize`. A screenshot captured the taxid preset and successful response.
- Live ACR safety evidence: runs `de91` and `de94` failed before deployment while discovering the
  missing tool and incorrect task syntax; after each failure ACR returned to `Disabled/Deny` and AKS
  retained `elb-openapi:4.50` at 1/1 Ready. Run `de97` then succeeded and published
  `elb-openapi:4.51` at digest
  `sha256:c9678afd3d3e1feb7a5d1dd115154b1b6bdf270bf9b8c8310583262a184cd985`.
- Rebuild task `b2b1a6c1-716e-4da2-ab76-29b84657e9cc` chained deploy task
  `d8c843eb-5b03-47f7-869a-8ddb99f4d24b`; deployment generation 49 converged to
  `elb-openapi:4.51`, 1/1 Ready/Available, restart count 0, `/healthz=200`, and a 15-path OpenAPI
  document. The running `/app/main.py` contained the active database fallback and no obsolete guard.
- The same taxid preset used in the reported failure submitted without a search-space field and
  returned HTTP 202 as OpenAPI job `4cf5f7c1c4f6`, with active generation
  `ncbi-direct-20260819-cab30d18c360`. The low-priority validation job was immediately deleted:
  DELETE returned 200, status lookup returned 404, ConfigMap/Job/Pod counts were zero, and the
  528-byte query blob was removed successfully.
- Final network posture: both ACR and Storage report `publicNetworkAccess=Disabled` and
  `defaultAction=Deny`.