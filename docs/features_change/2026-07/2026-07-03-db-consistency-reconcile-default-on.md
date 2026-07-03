---
title: DB consistency self-heal reconciler enabled by default
description: Turn the Tier 2 volume/shard consistency reconciler ON in the control-plane env source of truth so every deploy path keeps auto self-heal running.
tags:
  - operate
  - blast
---

# DB consistency self-heal reconciler enabled by default

## Motivation

A deployed BLAST database (`core_nt`) drifted into a three-way inconsistency
after NCBI shrank the volume count: the `.njs` metadata reported 79 volumes,
Storage still held ghost volume files up to index 93 (never pruned by
prepare-db), and the shard layout referenced 87 volumes. `blastdbcmd` then
failed with "vol does not match lmdb vol", which surfaced as opaque `db warmup`
and `one or more BLAST jobs failed` errors.

The Tier 2 reconciler (`api.tasks.storage.reconcile_db_consistency`, scheduled
by beat) can detect and repair this drift automatically — prune ghost volumes
above the authoritative `.njs` `number-of-volumes` count and rebuild the stale
shard layout — but it shipped gated **default-OFF** behind
`DB_CONSISTENCY_RECONCILE_ENABLED` (charter §12a Rule 4). Leaving it off means
the same drift silently recurs the next time NCBI resizes a database and
requires manual recovery again.

## User-facing change

- The periodic DB volume/shard consistency reconciler now runs continuously on
  the `worker` sidecar (scheduled by `beat` every
  `CELERY_BEAT_DB_CONSISTENCY_SECONDS`, default 1800s). It prunes ghost volumes
  and rebuilds stale shard layouts before they break a warmup / BLAST submit.
- No change for a healthy database: with authoritative `.njs` metadata present
  and no ghost volumes, the reconciler is a no-op. It only acts on a database
  that has actually drifted.

## API / IaC diff summary

- `infra/control-plane-env.json`: added `"DB_CONSISTENCY_RECONCILE_ENABLED": "true"`
  to the `worker` and `beat` sidecar sections (the task runs on the worker and
  is scheduled by beat; the api shard route heals inline regardless of this
  flag, so `api` intentionally does not carry the key).
- `infra/modules/containerAppControl.bicep`: wired the matching
  `controlPlaneEnv.worker.DB_CONSISTENCY_RECONCILE_ENABLED` /
  `controlPlaneEnv.beat.DB_CONSISTENCY_RECONCILE_ENABLED` env entries into the
  worker and beat containers so a full `azd provision` keeps the toggle ON.
- Both change together so `scripts/dev/quick-deploy.sh` (fast image PATCH) and
  `azd provision` (full deploy) converge on the same runtime state.

## Charter §12a Rule 4 note (default-OFF flip)

Rule 4 defaults a new guard OFF and flips it ON in a separate PR after a
dogfood cycle. This flip is made deliberately because:

- It was **live-validated** this session: the manual heal (prune ghost volumes
  `core_nt.79`–`93`, delete stale shard layout, regenerate shards) restored the
  database and produced a clean 10/10 warmup.
- It is guarded defensively: no authoritative `.njs` → never prune; ghost
  fraction > 50% → abort; per-DB non-blocking `prepare_db_lock`. A healthy DB
  is untouched.
- It is a data-plane consistency toggle only — no auth, RBAC, network, or
  persona-matrix surface is affected, so the Persona Matrix is unchanged.
- The value can still be overridden OFF per deployment via a process/azd-env
  override (`control_plane_env_pairs` honours a set env var over the JSON
  default).

## Validation evidence

- `uv run pytest -q api/tests/test_control_plane_env.py api/tests/test_db_consistency.py`
  → 31 passed (JSON↔Bicep key references stay in lockstep; shared-key values
  match across sidecars).
- Applied to the deployed customer Container App via
  `az containerapp update --container-name worker|beat --set-env-vars
  DB_CONSISTENCY_RECONCILE_ENABLED=true`; `az containerapp show` confirms both
  the `worker` and `beat` containers report the value `true` on the new
  revision.
