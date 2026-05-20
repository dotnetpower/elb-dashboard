# Submit Slower Profile Confirmation

## Motivation

When a database is already warmed and prepared shards are available, `Sharded throughput` is the preferred runtime profile. Switching to `Baseline` or `Warmed database` can disable shard parallelism and make large searches slower.

## User-facing change

The submit runtime panel now asks for confirmation before switching from an available sharded profile to `Baseline`, `Warmed database`, or the `Off` sharding mode. The dialog explains the slowdown risk and makes `Cancel` the first action so the recommended path keeps `Sharded throughput` selected.

## API/IaC diff summary

No API or IaC changes. Frontend-only confirmation flow in the BLAST submit runtime profile selector.

## Validation evidence

- `npm run build` in `web/`.
- `npx eslint src/components/ConfirmDialog.tsx src/pages/blastSubmit/ComputeSection.tsx --max-warnings 0` in `web/`.