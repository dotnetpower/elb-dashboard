# Fix the full-DB memory block catch-22 for not-warm databases (node_disk)

## Motivation

A user with a 10-node `Standard_E16s_v5` cluster switched the Performance
warm-cache mode to **Node disk** and then could not submit a `core_nt` search.
The "Required before submitting" checklist showed the full-database memory
block and told them to *"Switch to the Sharded throughput execution profile"* —
but that profile was greyed out, so there was no way forward (catch-22).

Root cause (diagnosed end-to-end):

- `warm_cache_mode` never reaches the submit page. The link is indirect: when
  `core_nt` is **not warm** on the selected cluster,
  `deriveShardingAvailability` disables every sharded profile, the effective
  execution profile collapses to `off`, and `deriveFullDbMemoryFit` then blocks
  the full-DB run (core_nt 249.7 GB vs 126 GB usable per node).
- `node_disk` makes this state common: its stable VMSS instance names keep
  pre-stop / `Failed` warmup Jobs pinned (no node-rotation self-heal like
  `ephemeral`), and the RAM page cache is cold after a stop/start until vmtouch
  re-runs — so the DB reports not-`Ready` and sharding stays disabled.
- ElasticBLAST's own pre-flight only relaxes the RAM requirement via sharding
  partitions, never via disk mode, so the memory gate itself is correct — the
  defect was purely the dead-end remediation message.

## User-facing change

When the full-DB memory block fires **only because the database is not warm
yet** (and warming would unlock a sharded profile that fits, and warming is
feasible on the cluster), the blocker now steers the user to the actionable
step — warm the database — instead of pointing at the greyed-out Sharded
control:

> 'core_nt' needs 249.7 GB and does not fit a single node's 126 GB usable for a
> full-database BLAST. Warm this database on the selected cluster to enable the
> Sharded throughput profile, which spreads it across your nodes — or use a
> cluster with a larger machine type.

When sharding genuinely cannot help (too few nodes, no prepared shard layout,
DB too large even when sharded) or warmup is infeasible, the original
larger-machine remediation is kept unchanged. No memory-fit verdict changed —
the same runs are blocked/allowed as before; only the remediation text adapts.

## API / IaC diff summary

- `web/src/pages/blastSubmit/shardingAvailability.ts` — new
  `canUnlockShardingByWarming` field on `ShardingAvailability`, computed by
  re-running the precondition chain with warm forced on (additive, backward
  compatible).
- `web/src/pages/blastSubmit/memoryFit.ts` — new pure helper
  `fullDbMemoryWarmupRemediation`; `deriveFullDbMemoryFit` itself is unchanged.
- `web/src/pages/BlastSubmit.tsx` — resolves the final
  `fullDbMemoryBlockedReason` from the two signals, with a defensive fallback to
  the default message so a block can never be silently dropped.
- No backend change: `_gate_node_memory_fit` is a post-submit safety net that
  never sees the greyed-out-control UI state, so its message stays correct.

## Validation evidence

- `npx vitest run src/pages/blastSubmit/memoryFit.test.ts
  src/pages/blastSubmit/shardingAvailability.test.ts
  src/pages/blastSubmit/submitValidation.test.ts` — 36 passed (3 new
  `fullDbMemoryWarmupRemediation` cases + 4 new `canUnlockShardingByWarming`
  cases).
- `npx vitest run src/pages/blastSubmit` — 184 passed (was 177).
- `npm run build` — succeeds.
- `eslint` on the changed files — clean.
