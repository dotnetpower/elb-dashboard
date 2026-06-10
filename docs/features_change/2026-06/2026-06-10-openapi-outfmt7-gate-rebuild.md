---
title: OpenAPI elb-openapi 4.21 rebuild for sharded outfmt 7
description: Rebuild the elb-openapi image from the patched local context so OpenAPI-plane sharded submits accept outfmt 7 (taxid/scientific-name columns).
tags:
  - blast
  - release
---

# OpenAPI `elb-openapi` 4.21 rebuild for sharded `outfmt 7`

## Motivation

An OpenAPI-plane submit (`submission_source: external_api`, the API Reference
"Mode B — core_nt tabular + taxids" try-it) of
`-outfmt 7 std staxids sscinames` failed with:

```
ERROR: Partitioned BLAST requires outfmt 5 without extended fields,
outfmt 6, or "6 std..."; 7 is not supported for merge
```

Root cause: the deployed `elb-openapi` image was tag **4.20**, pinned and built
on 2026-06-04. The three patches that make sharded `outfmt 7` work —

1. `patch_partitioned_outfmt_gate` (widen ElasticBLAST's `elb_config.py`
   partitioned-outfmt gate to allow `7` / `7 std`),
2. the quote-safe multi-token `-outfmt` argv rebuild in `blast-run-aks.sh`
   (`patch_blast_run_aks_outfmt_argv`), and
3. the field-aware shard merge (`resolve_tabular_columns` in
   `merge-sharded-results.sh`)

— all landed on 2026-06-10 (commits `86e2c5e` and `4bd037a`), **after** the 4.20
image was built. The `docker-openapi` build context still carried the 2026-06-04
copies of `patch_elastic_blast.py` and `merge-sharded-results.sh`, which contain
none of the three patches (`grep` counts were 0/0/0). The dashboard (terminal
sidecar) path was already correct because its image was rebuilt by CI on the
latest commit; only the separately-pinned OpenAPI plane was stale.

## User-facing change

OpenAPI-plane sharded BLAST submits (`/v1/jobs` and the API Reference try-it)
now accept `-outfmt 7 std staxids sscinames` and produce merged tabular results
with taxid + scientific-name columns for NCBI databases that ship a taxdb (e.g.
`core_nt`). No dashboard-submit behaviour changes.

## API / IaC diff summary

- `api/services/image_tags.py`: `IMAGE_TAGS["elb-openapi"]` bumped `4.20` →
  `4.21`. 4.21 is the same upstream app code (3.7.5) rebuilt from the patched
  local context to pick up the gate/argv/merge patches.
- `~/dev/elastic-blast-azure/docker-openapi/{patch_elastic_blast.py,
  merge-sharded-results.sh}` refreshed from this repo's `terminal/` copies (the
  build-context support files; not tracked in this repo). The committed
  Dockerfile already wires `python3 /tmp/patch_elastic_blast.py` at
  `ELB_REF=7a471297`, whose `elb_config.py` gate block matches the patch anchor
  verbatim, so the patch applies cleanly at build time.

## Build + rollout

Rollout order (charter): build+push the sibling image to ACR FIRST, then move
the pin.

```bash
cp terminal/patch_elastic_blast.py   ~/dev/elastic-blast-azure/docker-openapi/
cp terminal/merge-sharded-results.sh ~/dev/elastic-blast-azure/docker-openapi/
az acr build -r acrelbdashboard3abp67bppe -t elb-openapi:4.21 \
  -f ~/dev/elastic-blast-azure/docker-openapi/Dockerfile \
  ~/dev/elastic-blast-azure/docker-openapi
# then bump IMAGE_TAGS["elb-openapi"] = "4.21" and redeploy the elb-openapi pod
```

## Validation evidence

- `git merge-base --is-ancestor` confirms the 4.20 pin (`6a818b6`, 2026-06-04)
  predates the gate patch (`86e2c5e`, 2026-06-10) — the staleness root cause.
- Context `grep` before refresh: gate=0, argv=0, merge=0; after refresh: 2/2/2.
- ElasticBLAST `elb_config.py` gate block at `7a471297` matches the patch anchor
  verbatim (so the build-time patch applies without drift).
- Post-deploy: re-run the Mode B `outfmt 7 std staxids` OpenAPI submit and
  confirm the job no longer fails at the partitioned-outfmt gate.
