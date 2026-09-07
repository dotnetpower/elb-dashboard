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
- The ACR pre-build command now runs in a pinned tool image containing git and Python. The first
  live `4.51` build correctly failed before deployment because ACR's implicit command image had no
  `git`; the build-first gate preserved the running `4.50` deployment and restored ACR to
  `publicNetworkAccess=Disabled`, `defaultAction=Deny`.
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