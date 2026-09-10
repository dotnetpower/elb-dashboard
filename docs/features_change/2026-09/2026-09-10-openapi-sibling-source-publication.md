---
title: OpenAPI sibling source publication
description: Publish the validated OpenAPI runtime contracts in the sibling repository and align the next dashboard image lineage.
tags:
  - blast
  - architecture
  - security
---

# OpenAPI sibling source publication

## Motivation

The deployed `elb-openapi:4.55` runtime was reproducible from a reviewed sibling
commit plus the dashboard build-context patcher, but the public
`dotnetpower/elastic-blast-azure` `master` branch did not itself contain the
result-readiness, selection, exact-oracle, and reference-context contracts.
External reviewers therefore could not inspect the complete runtime from the
sibling repository alone.

Publication review also found one valid edge case that crossed the resolver,
typed response, dashboard validation, and exact-oracle layers: a short-query
reference result may report `length_adjustment=0`. The parser accepted it, but
the prior typed schema and downstream validators required a positive value.

## User-facing change

- Sibling commit `8c0bacf` publishes the OpenAPI runtime source, direct tests,
  and [runtime contract document](https://github.com/dotnetpower/elastic-blast-azure/blob/master/docs/openapi-runtime-contracts.md).
- The public source now contains canonical-merge readiness, the bounded
  `finalizer_failed` transition, native/diversity result selection, active
  generation and exact-oracle validation, and the authenticated reference
  context resolver.
- A valid zero `length_adjustment` is accepted consistently by resolver,
  request/response schemas, dashboard validation, and exact-oracle execution.
- A legacy top-level statistical context can no longer bypass the
  `diversity_aware` incompatibility guard.
- One-shard manifests and merge shard counts are bounded to 1,024 entries.
- Existing requests that omit the new fields retain `native_top_n` and the
  prior API behavior.

## Build and deployment summary

- `OPENAPI_SIBLING_SOURCE_REF` advances from `352a1f4` to the published
  `8c0bacf97ce9df9b4266e54338e42f1cd502948c` commit.
- The next immutable image target is `elb-openapi:4.56`. The dashboard patcher
  remains in the build as an idempotent compatibility and safety assertion.
- The currently deployed `elb-openapi:4.55` image is unchanged and remains the
  rollback boundary. This source-publication change does not build or deploy
  4.56 and does not modify Azure, RBAC, or network configuration.

## Validation

- Sibling `docker-openapi/tests`: 149 passed in an isolated Python 3.11
  environment before and after publication review.
- Dashboard full backend suite: 5,812 passed, with 4 environment-dependent
  evidence checks skipped.
- Dashboard affected contract sweep: 331 passed; contract and patcher tooling:
  52 passed.
- Sibling Python compile, merge-helper `bash -n`, Ruff, and diff checks passed.
- A fresh clone of remote sibling `master` resolved to `8c0bacf`; applying the
  dashboard patcher produced no source diff, and a second application remained
  idempotent.
- Focused cross-repository zero-adjustment, selection-conflict, manifest-bound,
  and merge-shard-bound tests passed.
- The OpenAPI baseline changed only `length_adjustment.minimum` from 1 to 0.
  Previous-HEAD compatibility reported no breaking change, and generated
  TypeScript types remained current.
- The production mypy debt baseline, Ruff, docs frontmatter guard, and strict
  MkDocs build passed.