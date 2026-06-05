# Full-DB memory block: explain why node count does not help

## Motivation

A user with a 10-node cluster hit the full-database (non-sharded) memory block
on `core_nt` and read it as a contradiction: the message said the cluster node
(`Standard_E16s_v5`) "provides only 126 GB usable" while they had ten such nodes
active. The block itself is correct — a full-database BLAST loads the entire
database into **each** node, so node count gives query parallelism, not memory
relief, and ElasticBLAST's own submit pre-flight rejects the same run — but the
wording never said so, making a correct safety gate look like a bug.

## User-facing change

The full-DB memory block message (shown in the "Required before submitting"
checklist and the submit summary rail) now states that a full-database BLAST
loads the whole database into a single node, that adding more nodes does not
help, and that the Sharded throughput profile spreads the database across the
available nodes. No logic changed — the same runs are blocked/allowed as before.

Before:

> 'core_nt' needs 249.7 GB for a full-database BLAST but the cluster node
> (Standard_E16s_v5) provides only 126 GB usable (128 GB RAM minus 2 GB system
> reserve). Switch to the Sharded throughput execution profile, or use a cluster
> with a larger machine type.

After:

> 'core_nt' needs 249.7 GB for a full-database BLAST, which loads the entire
> database into a single node — adding more nodes does not help. The cluster
> node (Standard_E16s_v5) provides only 126 GB usable (128 GB RAM minus 2 GB
> system reserve). Switch to the Sharded throughput execution profile to spread
> the database across your nodes, or use a cluster with a larger machine type.

## API / IaC diff summary

- `web/src/pages/blastSubmit/memoryFit.ts` — `deriveFullDbMemoryFit` blocked-reason
  wording (frontend client mirror of the gate).
- `api/services/blast/submit_gates.py` — `_gate_node_memory_fit` blocking message
  kept in lockstep with the frontend. The `action` / `action_type` fields and all
  gate verdicts are unchanged.

## Validation evidence

- `uv run ruff check api/services/blast/submit_gates.py` — clean.
- `uv run pytest -q api/tests/test_blast_submit_gates.py` — 35 passed (asserts on
  `error_code`, `action_type`, and the preserved `"system reserve"` substring).
- `npx vitest run src/pages/blastSubmit/memoryFit.test.ts src/pages/blastSubmit/submitValidation.test.ts`
  — 17 passed.
- `npm run build` — succeeds.
