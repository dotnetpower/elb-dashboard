# Submit Sharded Throughput Default

## Motivation

The BLAST submit runtime panel could show prepared shards and safe capacity while keeping the run profile on `Warmed database`. That made the default selection contradict the availability message.

## User-facing change

When a selected database is already warm, has prepared shards, and fits the selected cluster, the submit form now promotes the run profile to `Sharded throughput` automatically. If the user explicitly chooses `Off`, `Baseline`, or `Warmed database`, that opt-out is preserved.

## API/IaC diff summary

No API or IaC changes. Frontend-only reconciliation of submit runtime form state.

## Validation evidence

- `npm run test -- shardingAvailability.test.ts --run` in `web/`.
- `npm run build` in `web/`.