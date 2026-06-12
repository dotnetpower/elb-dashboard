---
title: Durable cross-sidecar cache for NCBI esummary / GenBank lookups
description: A best-effort ops-Redis cache backs the Sequence Detail esummary and GenBank views so a cold api replica reuses a payload another sidecar already fetched instead of paying the 10-16s NCBI efetch again.
tags:
  - research
  - architecture
---

# Sequence Detail — Durable GenBank/esummary Cache (#27)

## Motivation

Large NCBI nucleotide records (e.g. `PQ221797.1`, a ~197 kb Monkeypox virus
genome) make the Sequence Detail page slow: the GenBank efetch XML embeds the
full sequence plus every CDS translation, so NCBI takes 10-16 s to generate it.
The 503-on-timeout half of this issue was already fixed by raising the efetch
client timeout. The remaining cost is that the existing TTL cache in
[api/services/ncbi/nuccore.py](../../../api/services/ncbi/nuccore.py) is
**per-process**: it resets on every api/worker restart and is not shared across
replicas, so the first viewer on a cold replica always re-pays the full efetch.

## User-facing change

No visible UI change. The first viewer of a record after an api sidecar restart
(or on a different replica) now gets the cached payload — the page loads in
milliseconds instead of waiting 10-16 s for NCBI — when another sidecar already
fetched it within the cache window. Behaviour is unchanged when the cache is
cold or Redis is unreachable.

## API / IaC diff summary

Backend-only, additive:

* [api/services/ncbi/nuccore.py](../../../api/services/ncbi/nuccore.py) — a
  second, durable cache layer (ops Redis, JSON, 7-day TTL) backs
  `fetch_nuccore_summary` and `fetch_nuccore_genbank`. Reads re-seed the
  in-process LRU so subsequent reads on the same replica skip Redis. The 7-day
  TTL is safe because a GenBank record for a fixed `accession.version` is
  immutable once published.
* **Best-effort, never changes correctness:** any Redis error — or
  `NCBI_DURABLE_CACHE_DISABLED=true` — degrades silently to the in-process
  cache + live NCBI fetch. Payloads larger than 1 MiB are not persisted to keep
  ops Redis lean (the in-process LRU still serves them within a replica).

No IaC change — `OPS_REDIS_URL` is already wired for the autostop status cache.

## Validation evidence

* `uv run pytest -q api/tests/test_ncbi_nuccore.py` → 61 passed, including three
  new tests:
  * `test_durable_cache_survives_in_process_reset` — second viewer after an
    in-process cache wipe is served from Redis with no second NCBI fetch.
  * `test_durable_cache_degrades_on_redis_error` — Redis get/setex raising still
    yields a correct live-fetch result.
  * `test_durable_cache_disabled_env_skips_redis` — the kill-switch never touches
    Redis.
* `uv run pytest -q api/tests` → 3319 passed, 3 skipped.
* `uv run ruff check api` → clean.
* An autouse test fixture disables the durable cache by default so the existing
  in-process `cached` assertions stay deterministic regardless of whether a
  local Redis is reachable.
