# Auto-warm re-enqueues stale DBs and OpenAPI Update panel hides when up to date

## Motivation

Two regressions surfaced from the deployed dashboard
(`ca-elb-control.gentlemeadow-01289e5b.koreacentral.azurecontainerapps.io`):

1. **`core_nt` showed as Not warm even though Auto warm was enabled and the
   AKS cluster was ready.** The beat-driven `reconcile_auto_warmup` task was
   firing every minute but every tick logged
   `{'status': 'already_ready'}` and never enqueued a warmup. Once the
   preference's `last_ready` flag flipped to `True`, the reconcile
   short-circuited with `if pref.last_ready and not force: continue` and
   never re-evaluated per-DB state. Newly downloaded DBs (or DBs whose
   warmup pod was deleted/failed after the cluster's first ready edge) were
   stuck cold forever until a manual click.
2. **The "Update OpenAPI to v4.9" button was shown on the API Reference
   page even when the deployed pod was already serving v4.9.** The panel
   rendered unconditionally whenever the OpenAPI service was reachable
   and the ACR image existed, with no comparison against the deployed
   `info.version` from the spec.

## User-facing change

- `core_nt` (and any other DB the user toggled into Auto warm) now gets
  enqueued automatically on the next beat tick after the cluster becomes
  ready *and* the DB is downloaded but not yet warm. No manual click
  needed, even if the cluster had been ready for a while.
- The "Update OpenAPI" panel only appears when the dashboard's pinned
  image tag (`api/services/image_tags.py` `IMAGE_TAGS["elb-openapi"]`)
  differs from the version the running pod reports through its OpenAPI
  spec's `info.version`. When they match, the panel is hidden entirely.

## API / IaC diff summary

Backend (`api/tasks/storage.py`):
- Removed the `pref.last_ready and not force` short-circuit inside
  `reconcile_auto_warmup`. The per-DB `warm_status in {Ready, Loading}`
  check below already prevents enqueuing DBs that are actually warm, so
  re-running the loop every beat tick is cheap and correct.
- Added a Redis-backed `_autowarmup_inflight_acquire` lock
  (`autowarmup:inflight:<sub>:<rg>:<cluster>:<db>` in ops Redis db 2,
  15 min TTL, fail-open). This prevents duplicate enqueues during the
  pre-Kubernetes phases of `warmup_database` (download / shard / plan)
  while `k8s_warmup_status` cannot yet observe the warmup pod.

Tests (`api/tests/test_auto_warmup.py`):
- New `test_reconcile_auto_warmup_reenqueues_when_db_not_warm` covers a
  persisted preference with `last_ready=True` and confirms that a cold
  DB gets re-enqueued *without* `force=True`.
- New `test_reconcile_auto_warmup_inflight_lock_prevents_duplicate`
  proves the new Redis lock skips the enqueue and returns
  `{reason: "inflight"}` in the skipped list.
- Existing tests monkeypatch the new inflight helper to `True` so the
  no-Redis test environment can still validate the enqueue path.

Frontend (`web/src/pages/ApiReference.tsx`):
- Wrapped the `<OpenApiDeployPanel variant="update" />` render in a
  guard that returns `null` when
  `acrQuery.data?.expected_image_tags?.["elb-openapi"]` equals
  `spec?.version`. The previously read `currentTag` (latest tag in ACR)
  is not a reliable indicator of what the *running* pod serves, so we
  use the live spec instead.

No IaC, schema, or Bicep changes. No new dependencies (the inflight lock
re-uses the existing `OPS_REDIS_URL` already wired into every sidecar
via `redis` from `pyproject.toml`).

## Validation evidence

```
$ uv run pytest -q api/tests/test_auto_warmup.py
.....                                                                    [100%]
5 passed in 2.08s

$ uv run pytest -q api/tests
... 643 passed in 56.51s

$ uv run ruff check api/tasks/storage.py api/tests/test_auto_warmup.py
All checks passed!

$ cd web && npm run build
✓ built in 7.13s
```

Production symptom that triggered the fix
(`az containerapp logs show -n ca-elb-control -g rg-elb-ca --container worker`):

```
Task api.tasks.storage.reconcile_auto_warmup[…] succeeded in 0.39s:
  {'status': 'completed',
   'clusters': [{'cluster_name': 'elb-cluster',
                 'databases': [...],
                 'enqueued': [...],
                 'skipped': [...],
                 'status': 'already_ready'}]}
```

After the fix, reconcile will replace `'already_ready'` with `'triggered'`
on the first tick where a configured DB is not yet warm, and `'ready_noop'`
on subsequent ticks once the warmup pod is `Loading`/`Ready`.
