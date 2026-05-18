# Evidence-Aware Sharding Claim

## Motivation

The submit page could label precise sharding as `Web-equivalent shard` whenever a warmed prepared database fit the selected cluster. That over-claimed equivalence for databases without verified Web BLAST search-space evidence.

## User-Facing Change

The submit page now uses `Web-equivalent shard` only when the selected database row includes a verified `web_blast_searchsp` value. Databases without verified evidence show `Precise shard` as disabled with an explicit verified-evidence blocker, while `Fast shard` remains available as a non-equivalence throughput probe.

## API/IaC Diff Summary

No API or IaC changes. The frontend sharding availability model now derives precise-mode availability from the database evidence metadata already returned by `/api/blast/databases`.

## Validation Evidence

- `npm run test -- src/pages/blastSubmit/shardingAvailability.test.ts` -> `5 passed`.
- `EQ-09/EQ-10` focused backend equivalence tests remain green: `46 passed in 0.72s`.
