# Auto Warm Sharded Throughput Defaults

## Motivation

Auto warm preferences indicate that a database should be kept ready for fast runs. Once such a database is warm and has prepared shard layouts, the submit page should prefer the sharded path instead of staying on the slower warmed/full-DB profile.

## User-facing change

The submit page now observes the local Auto warm database preference list. When the selected Auto warm database is already warm on the selected cluster and sharded throughput is available, the run profile is promoted to Sharded throughput automatically. The AKS card also surfaces active per-database messages, such as copying progress or stale warm-cache state, directly under the database chips instead of hiding them only in hover text.

Warmup progress now uses azcopy's per-pod copy percentage while shards are still being copied, so progress bars move before any whole node reaches `Ready`.

Warmup row labels now separate Storage state from AKS cache state: Storage shows `Storage DB ready`, while the node-local work shows `AKS cache copying`, `AKS cache ready`, or related cache-specific labels.

## API/IaC diff summary

- Backend warmup status now derives `progress_pct` from active pod copy logs when available.
- Frontend submit-profile reconciliation now treats Auto warm selection as a signal to prefer sharded throughput once ready.
- Frontend warmup labels now distinguish Storage DB readiness from AKS node-local cache progress.
- No IaC changes.

## Validation evidence

- `cd web && npm run test -- src/pages/blastSubmit/shardingAvailability.test.ts src/components/ClusterItem/DatabaseChipStrip.test.ts`
- `cd web && npm run test -- src/components/warmupSection/helpers.test.ts`
- `uv run pytest -q api/tests/test_warmup_jobs.py -k azcopy_percent`
- `cd web && npm run build`