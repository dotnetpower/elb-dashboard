# Submit Runtime Loading Stability

## Motivation

The BLAST Submit page could render a partially resolved runtime profile while database metadata, warmup status, and cluster context were still loading. In that transient state, selecting `Warmed database` could later flip to `Sharded throughput` once shard metadata arrived, which made the run profile feel unstable right before submission.

## User-facing change

The database and execution profile sections now show skeleton loading states until their backing data is ready. `Warmed database` remains a stable explicit choice, and the page no longer auto-upgrades an `off` sharding selection into a sharded mode after metadata finishes loading.

## API/IaC diff summary

- Added database-list skeleton rendering while `/api/blast/databases` is loading.
- Added execution-profile skeleton rendering while cluster, database metadata, or warmup status is still loading.
- Kept baseline/off sharding selectable for warmed databases.
- Limited automatic sharding-mode correction to already-selected sharded modes that become invalid.
- Blocked the submit button while runtime metadata is still loading.
- No backend API or IaC changes.

## Validation evidence

- `cd web && npm run test -- shardingAvailability.test.ts submitValidation.test.ts --run` — 10 passed.
- `cd web && npx eslint src/pages/BlastSubmit.tsx src/pages/blastSubmit/ComputeSection.tsx src/pages/blastSubmit/DatabaseSection.tsx src/pages/blastSubmit/shardingAvailability.ts src/pages/blastSubmit/shardingAvailability.test.ts src/pages/blastSubmit/submitValidation.ts src/pages/blastSubmit/submitValidation.test.ts src/pages/blastSubmit/types.ts --max-warnings 0` — passed.
- `cd web && npm run build` — passed; Vite emitted the existing large chunk warning.
