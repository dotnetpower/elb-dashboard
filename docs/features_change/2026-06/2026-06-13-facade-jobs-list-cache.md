---
title: Cache the direct external-BLAST jobs facade
description: >-
  The `/api/v1/elastic-blast/jobs` facade now shares the external-jobs TTL +
  negative cache, so a stale or unreachable OpenAPI base URL no longer costs a
  full list timeout then 503 on every poll.
tags:
  - blast
  - operate
---

# Cache the direct external-BLAST jobs facade

## Motivation

Issue [#30](https://github.com/dotnetpower/elb-dashboard/issues/30) tracked the
slow `/v1/jobs` listing surfaced through the dashboard. The investigation listed
the **direct facade route** (`GET /api/v1/elastic-blast/jobs` →
`external_blast.list_jobs()`) as candidate fix #3: it had **no cache**, so a
stale or unreachable OpenAPI base URL cost the full `_LIST_TIMEOUT_SECONDS`
(5 s) and then a 503 on **every** poll. The combined `/api/blast/jobs` route was
already cached (`_external_list_jobs_cached`, 70 s TTL + negative cache +
in-flight de-duplication), so the direct facade felt slower by comparison.

## User-facing change

- The direct facade `GET /api/v1/elastic-blast/jobs` listing is now served
  through the same shared external-jobs cache the combined route uses. Repeat
  polls within the TTL are served from memory; a failing sibling is negatively
  cached so polling does not keep paying the upstream round-trip to learn the
  same failure.
- Response shape is unchanged (`{"jobs": [...], "count": N}`); the 503
  `openapi_unreachable` error contract is unchanged (now short-circuited by the
  negative cache instead of re-hitting the upstream).

## API / IaC diff summary

- [api/routes/elastic_blast.py](../../../api/routes/elastic_blast.py)
  `list_external_blast_jobs` now calls
  `external_blast.external_jobs._external_list_jobs_cached({})` and rebuilds the
  `{"jobs": rows, "count": len(rows)}` envelope, instead of calling
  `external_blast.list_jobs()` synchronously on every request.
- No IaC change.

## Scope notes

- The biggest #30 cost (per-cluster `k8s_get_service_ip` 10 s timeout on
  Stopped/unreachable clusters) is already mitigated by the existing
  `_cluster_power_state_allows_openapi` gate in `_discover_subscription_clusters`
  (test `test_discover_subscription_clusters_skips_stopped`).
- httpx connection reuse for the OpenAPI plane (candidate fix #4) is **deferred**
  to the per-cluster resolver work in
  [#26](https://github.com/dotnetpower/elb-dashboard/issues/26): with the facade
  now cached, the upstream call rate drops from per-poll to ~once per TTL, so
  connection reuse is now the lowest-value remaining item and it tangles with
  the per-cluster base-URL scoping #26 introduces.
- Sibling-side cold-start / event-loop blocking (candidate fix #5) lives in the
  `elastic-blast-azure` repo and is out of scope here.

## Validation evidence

- `uv run pytest -q api/tests/test_external_blast_api.py` — 79 passed.
  New tests: `test_external_blast_facade_list_is_cached` (two polls → one
  upstream hit), `test_external_blast_facade_list_caches_upstream_failure`
  (failing sibling negatively cached → one upstream hit).
- `uv run pytest -q api/tests` — 3438 passed, 3 skipped.
- `uv run ruff check api/routes/elastic_blast.py api/tests/test_external_blast_api.py`
  — all checks passed.
