---
title: Rebuild elb-openapi 4.22 so the finalizer preserves the outfmt 7 '# Fields:' header
description: >-
  elb-openapi is rebuilt to 4.22 from the dashboard-patched sibling context so
  the shard finalizer keeps the per-shard '# Fields:' line, letting sharded
  outfmt 7 taxid / scientific-name columns reach merged_results.out.gz and the
  dashboard.
tags:
  - blast
  - operate
---

# Rebuild elb-openapi 4.22 so the finalizer preserves the outfmt 7 '# Fields:' header

## Motivation

Issue [#31](https://github.com/dotnetpower/elb-dashboard/issues/31): the dashboard
fix for sharded `outfmt 7` taxid / scientific-name columns has two halves with
different rollout paths. The **parser alias** half ships in the `elb-api` image
via the normal Container App deploy (already covered). The **finalizer
`# Fields:`-preservation** half is baked into the `elb-openapi` image, because the
finalizer + merge scripts are delivered to the cluster as a ConfigMap built from
that image. The deployed `elb-openapi:4.21` predated the finalizer change
(`terminal/patch_elastic_blast.py`: `awk '!/^#/'` → `awk '/^# Fields:/ || !/^#/'`),
so the merged `merged_results.out.gz` carried a std-12 `# Fields:` header over
14-column data rows and the dashboard Scientific Name column stayed blank for
OpenAPI-plane runs.

## User-facing change

- `elb-openapi` is rebuilt to **4.22** from the dashboard-patched local sibling
  build context (`scripts/dev/patch-openapi-build-context.py` support files
  refreshed from `terminal/`), so the in-cluster finalizer now runs
  `awk '/^# Fields:/ || !/^#/'` and keeps the per-shard `# Fields:` line.
- The merged result's `# Fields:` header now lists the extended columns
  (`... bit score, subject tax ids, subject sci names`), so the BLAST Results
  **Scientific Name** column + **Taxonomy** tab populate for sharded
  OpenAPI-plane runs instead of showing blank / "Unknown".
- The same rebuild also carries the multi-token `-outfmt` argv patch
  (`blast-run-aks.sh` rebuilds `ELB_BLAST_ARGV` and rejoins the `-outfmt` tokens),
  so an unquoted extended layout survives the YAML→env→shell→blastn path (issue
  #29 runtime half).

## API / IaC diff summary

- [api/services/image_tags.py](../../../api/services/image_tags.py) —
  `IMAGE_TAGS["elb-openapi"]` `4.21` → `4.22`.
- `~/dev/elastic-blast-azure/docker-openapi/patch_elastic_blast.py` refreshed from
  `terminal/patch_elastic_blast.py` (build-context support file; not committed to
  this repo).
- No Bicep / Container App template change.

## Rollout order (charter)

Build + push the sibling image to ACR FIRST, then move the pin:

1. `cp terminal/patch_elastic_blast.py ~/dev/elastic-blast-azure/docker-openapi/`
   (sync the finalizer fix into the build context).
2. `az acr build -r acrelbdashboard3abp67bppe -t elb-openapi:4.22 -f ~/dev/elastic-blast-azure/docker-openapi/Dockerfile ~/dev/elastic-blast-azure/docker-openapi`
   — ACR run `defw` Succeeded.
3. Bump `IMAGE_TAGS["elb-openapi"]` 4.21 → 4.22 (this commit).
4. Redeploy the pod: `kubectl set image deployment/elb-openapi openapi=…/elb-openapi:4.22`.

## Validation evidence

- New pod `elb-openapi-567675dbb7-…` runs `acrelbdashboard3abp67bppe.azurecr.io/elb-openapi:4.22`.
- Finalizer patch live in the pod's installed elastic-blast:
  `/opt/venv/lib/python3.11/site-packages/elastic_blast/templates/scripts/elb-finalizer-aks.sh:164`
  → `if ! zcat "$f" | awk '/^# Fields:/ || !/^#/' >> "$MERGE_INPUT"; then`.
- argv patch live: `blast-run-aks.sh` carries `ELB_BLAST_ARGV` rebuild (lines 86-130).
- Live sharded `core_nt` `-outfmt 7 std staxids sscinames` submit on `elb-cluster-01`
  (OpenAPI plane, job `a4a2ee33aeee`, `core_nt_precise` → 5 shards):
  - The batch pod rendered the multi-token specifier as a **single** blastn
    argument: `blastn -db core_nt_shard_00 … -outfmt 7 std staxids sscinames -searchsp …`
    (argv patch works — no YAML break, no shell word-split).
  - `merged_results.out.gz` `# Fields:` header carries the extended columns:
    `… bit score, subject tax ids, subject sci names`.
  - Data rows are 14-column with the taxid + scientific name populated, e.g.
    `NR_024570.1 … 998  562  Escherichia coli` (taxid `562`).
  - `merge-report.json` resolved columns by name: `qseqid=0, subject=1, evalue=10,
    bitscore=11` (std-first keeps qseqid leading → single query group, not collapsed).
  - The dashboard results parser (`api/services/blast/results_parser.py`) already
    aliases `subject tax ids` → `staxids` and `subject sci names` → `sscinames`,
    so the Scientific Name column + Taxonomy tab populate from this header.
