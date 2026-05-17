# Submit Performance Sharding UX

## Motivation

The New Search compute section let users toggle warmup manually and choose sharding modes without clearly tying those choices to the selected database, cluster cache state, prepared shard layouts, output format, or shard capacity. That made unwarmed databases look eligible for precise sharding and left the warmup checkbox as a confusing user-facing control.

## User-facing change

The Compute Environment performance panel now treats warmup as database state instead of a checkbox. It shows whether the selected DB is already cached on the selected cluster and disables sharded performance modes until the DB is warm, has prepared shard layouts, uses merge-compatible outfmt 5 or 6, and fits the selected cluster's node count/RAM capacity.

The database selector also annotates entries with the current cluster cache state (`Warm n/n` or `Not warm`) when warmup status is available. For a warmed prepared-shard DB, `Off` is disabled so the submit path consumes the node-local shard cache instead of silently bypassing it.

The sharding mode selector now explains each mode in more detail:

- Off: safest full-DB baseline.
- Fast shard: warmed prepared shards with approximate merge semantics.
- Precise shard: warmed prepared shards with search-space correction and pre-flight metadata checks.

If capacity is known, the panel shows the selected shard count and per-shard GiB estimate. Existing session drafts are migrated away from stale warmup/sharding selections.

## API/IaC diff summary

No API or IaC change. This is a frontend-only update to the BLAST submit form state, Compute Environment panel, sharding capacity helper, and validation guard.

## Validation evidence

- `cd web && npm run test -- src/pages/blastSubmit/shardingAvailability.test.ts src/pages/blastSubmit/useDraftForm.test.ts` — 10 tests passed.
- `cd web && npm run build`
- Browser check on `http://127.0.0.1:8090/blast/submit`: warmup checkbox is hidden; the database selector marks `core_nt` as `Warm 10/10`; with `core_nt` warmed on `elb-cluster`, Off is disabled, Fast shard and Precise shard are enabled, and shard capacity shows `N=10`; with `16S_ribosomal_RNA` not warmed, Fast shard and Precise shard are disabled and the panel explains the warm/cache and capacity constraints.
