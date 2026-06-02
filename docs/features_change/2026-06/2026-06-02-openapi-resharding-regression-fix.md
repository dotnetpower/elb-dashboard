# 2026-06-02 — OpenAPI core_nt sharding regression fix (4.17 → 4.18)

## Motivation

An OpenAPI `POST /api/v1/elastic-blast/submit` of `core_nt` with
`resource_profile: core_nt_safe` failed pre-flight with:

```
memory requirements exceed memory available … Standard_E16s_v5 … at least 251.7GB
```

The same database submitted from the dashboard **New searches** path
(`POST /api/blast/submit`) succeeds. Investigation found a two-path
divergence:

* **Dashboard path** — `api/services/blast/config.py` `build_config`
  auto-injects `db-partitions`, `db-partition-prefix`, and
  `-searchsp 32156241807668` when the DB is sharded + warmed, so core_nt
  is split across nodes and fits the 124 GB `Standard_E16s_v5` memory
  floor.
* **OpenAPI path** — the request is proxied to the on-AKS `elb-openapi`
  pod (sibling `docker-openapi/app/main.py`). The deployed `4.17` image
  only **echoed** `resource_profile` and built a **full-DB** config (no
  sharding), so the 251.7 GB full `core_nt` exceeded node memory.

Root cause: the sharding translation lives only in
`scripts/dev/patch-openapi-build-context.py` (the dashboard runtime
policy patch). The `4.9` image that originally validated this
(2026-05-19) was built **from the patched local context**. The later
`4.17` rebuild (2026-05-30 P0 rollback follow-up) was built from the
**un-patched** sibling context, silently dropping the sharding
translation — a regression.

## User-facing change

* OpenAPI `resource_profile` values `core_nt_safe` / `core_nt_precise` /
  `precise` once again shard `core_nt` (≤ 10 partitions, node-pinned,
  Web BLAST-compatible `searchsp`) so the submit fits node memory and
  no longer fails the 251.7 GB pre-flight.
* No SPA / dashboard route change. The dashboard **New searches** path
  was always correct and is unchanged.

## API / runtime diff summary

| Surface | Change |
| --- | --- |
| sibling `docker-openapi/app/main.py` | Sharding translation re-applied via `patch-openapi-build-context.py` — `db-partitions = max(1, min(NUM_NODES, 10))`, `db-partition-prefix = {blob_base}/blast-db/{N}shards/core_nt_shard_`, `-searchsp 32156241807668` when no explicit `-searchsp`/`-dbsize`, accepted for `resource_profile ∈ {core_nt_precise, precise, core_nt_safe}`. |
| sibling `docker-openapi/Dockerfile` | `ELB_REF` bumped, `patch_elastic_blast.py` + `merge-sharded-results.sh` copied in and applied so the runtime can consume `db-partitions` and merge sharded results. |
| `api/services/image_tags.py` | `elb-openapi` pin `4.17` → `4.18` (the rebuilt, re-patched image). Comment block updated to record the regression and that the build MUST be produced from the patched local context. |

No IaC changes. Storage `publicNetworkAccess: Disabled` posture
unchanged. No new dependencies.

## Rollout order (followed)

1. `scripts/dev/patch-openapi-build-context.py ~/dev/elastic-blast-azure/docker-openapi`
   — patch the local sibling build context (sharding + runtime support).
2. `az acr build -r acrelbdashboard3abp67bppe -t elb-openapi:4.18 -f ~/dev/elastic-blast-azure/docker-openapi/Dockerfile ~/dev/elastic-blast-azure/docker-openapi`
   — build + push the re-patched image FIRST.
3. Bump `IMAGE_TAGS["elb-openapi"]` `4.17` → `4.18` in this repo.
4. Roll out `deployment/elb-openapi` on AKS (dashboard **Deploy
   elb-openapi** button or `kubectl set image` / `rollout restart`) so the
   pod pulls `4.18`.

> The 2026-05-30 P0 rollback note warns that inverting this order
> (`image_tags` ahead of a built tag) bricks the pod with
> `ImagePullBackOff: manifest unknown`. The order above keeps the pin
> behind the built image.

## Why "New searches" works but "API" failed (mechanism)

They are two independent code paths that build the ElasticBLAST config
differently:

| | New searches (dashboard) | API (OpenAPI) |
| --- | --- | --- |
| Route | `POST /api/blast/submit` | `POST /api/v1/elastic-blast/submit` |
| Config builder | `api/services/blast/config.py` `build_config` | sibling `docker-openapi/app/main.py` (on-AKS pod) |
| Sharding | auto-injected for sharded+warmed DB | **only** when the image was built with the dashboard patch |
| `4.17` deployed image | n/a | shipped WITHOUT the patch → full-DB → 251.7 GB pre-flight fail |

The submit example was correct; the deployed gateway image was the
defect.

## Validation evidence

- `scripts/dev/patch-openapi-build-context.py` applied cleanly to the
  sibling context; `grep` confirms `db-partitions` / `db-partition-prefix`
  / `core_nt_shard_` and `core_nt_safe` in the accepted profile set in
  `docker-openapi/app/main.py`, plus `patch_elastic_blast.py` +
  `merge-sharded-results.sh` copied and `ELB_REF` bumped in the Dockerfile.
- `az acr build … -t elb-openapi:4.18` — ACR run `de2y` succeeded in
  2m54s; pushed `acrelbdashboard3abp67bppe.azurecr.io/elb-openapi:4.18`
  digest `sha256:68c33e0843d25fa2d77ead2bf56a859165b4175ee2433c26471ddfef6331ab6a`.
- `uv run ruff check api/services/image_tags.py` — clean.
- `uv run pytest -q api/tests` — green.
- Live re-submit of OpenAPI `core_nt` + `resource_profile=core_nt_safe`
  on the rolled `4.18` pod — _persisted config `db-partitions` /
  `db-partition-prefix` / `searchsp` to be captured after rollout._

## Cross-references

- 2026-05-19 OpenAPI core_nt precise sharding (original patch + 4.9 build).
- 2026-05-30 OpenAPI pin P0 rollback (4.16 → 4.14) — rollout-order warning.
