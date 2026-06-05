---
title: Block full-DB BLAST that does not fit node memory before submit
description: Proactive frontend guard + backend submit gate that reject a non-sharded BLAST whose database exceeds the workload node's RAM, instead of failing at ElasticBLAST submit pre-flight.
tags:
  - blast
  - user-guide
---

# Block a full-DB BLAST that does not fit node memory (before it runs)

## Motivation

A dashboard BLAST submit against `core_nt` on a `Standard_E16s_v5` (128 GB)
cluster failed at runtime with ElasticBLAST's submit pre-flight rejection:

```
ERROR: BLAST database .../blast-db/core_nt/core_nt memory requirements exceed
memory available on selected machine type "Standard_E16s_v5". Please select
machine type with at least 251.7GB available memory.
```

Until now the dashboard only enriched the *post-failure* message with a
remediation hint (2026-06-02 change). The job still had to be submitted, queued,
and rejected before the user learned anything. There was **no validation that
blocked the run up front** — the deferred follow-up from that change.

## User-facing change

A full-database (non-sharded) BLAST is now blocked **before** submit when the
database cannot fit a single workload node's RAM:

- **Proactive frontend guard**: when the effective execution profile is `Off`
  (Baseline / Warmed database) and the selected database's memory footprint
  exceeds the cluster node's RAM, the **Run BLAST** button is disabled and the
  readiness list shows an actionable reason steering the user to the **Sharded
  throughput** profile (or a larger-machine cluster). When prepared shards are
  available the form already auto-promotes to Sharded throughput, so the block
  only bites when the full-DB path is genuinely the one selected.
- **Backend submit gate (defense in depth)**: `POST /api/blast/submit` now runs a
  `node_memory_fit` pre-flight gate that returns `409 blocked_by_preflight` for a
  non-sharded run whose database exceeds node RAM. This covers the OpenAPI and
  script submit paths too, not just the SPA.

The threshold mirrors ElasticBLAST's own submit pre-flight
(`elastic-blast-azure` `src/elastic_blast/elb_config.py`): it rejects when
`bytes_to_cache / 1024³ > node_ram_gib − SYSTEM_MEMORY_RESERVE`, where
`SYSTEM_MEMORY_RESERVE` is 2 GB (the OS headroom ElasticBLAST keeps). The guard
subtracts the **same** 2 GB reserve, so it neither false-blocks a database
ElasticBLAST would accept (e.g. `core_nt` ~251.7 GB on a `Standard_E32s_v5` /
256 GB node → 254 GB usable) nor lets through one it would reject — including the
2 GB boundary band `(RAM−2, RAM]` that a raw `required ≤ RAM` compare would
false-pass. When the requirement is unknown (no `bytes_to_cache` metadata) or the
node SKU's RAM is unrecognised, **nothing is blocked** — ElasticBLAST's own
pre-flight and the existing post-failure guidance remain the safety net.

## API / IaC diff summary

- `api/services/blast/submit_gates.py`: new `_gate_node_memory_fit()` gate +
  wired into `evaluate_submit_gates(..., submit_options=...)`. The gate resolves
  the sharding mode through the **same** `normalize_sharding_mode()` the INI
  generator uses, so a caller that omits `sharding_mode` but sets
  `db_auto_partition` / `allow_approximate_sharding` / `db_partitions` is treated
  as sharded here too (never false-blocked). Only the definitive over-RAM verdict
  is blocking (`status=fail`, `severity=critical`,
  `error_code=node_memory_insufficient`, `action_type=use_sharded_throughput`);
  every skip / unknown / probe-error / invalid-options path is non-blocking.
- `api/routes/blast/submit.py`: passes `submit_options=req.options` into
  `evaluate_submit_gates`.
- `web/src/api/blast.ts`: `BlastDatabase` gains optional `bytes_to_cache`.
- `web/src/pages/blastSubmit/memoryFit.ts` (new): pure `deriveFullDbMemoryFit()`
  mirroring the backend gate (`required <= nodeRam`, `fits: null` = unknown = no
  block).
- `web/src/pages/blastSubmit/submitValidation.ts`: new optional
  `fullDbMemoryBlockedReason` arg gates `canSubmit` and adds a `missing` entry.
- `web/src/pages/BlastSubmit.tsx`: computes the fit from the *effective* sharding
  mode and passes the reason into `deriveSubmitValidation`.
- No IaC, no new dependency. The `bytes_to_cache` field was already produced by
  `list_databases` (read from the BLASTDB `.njs` metadata); only the type and the
  consumers are new.

## Validation evidence

- `uv run pytest -q api/tests/test_blast_submit_gates.py` — 33 passed, including
  eight `node_memory_fit` cases (block on E16s_v5, pass on E32s_v5, skip when
  sharded, skip when `db_auto_partition` is set without an explicit mode, skip on
  invalid options, no-block when requirement/SKU unknown, non-blocking probe
  error).
- `uv run pytest -q api/tests` — 2795 passed (3 unrelated pre-existing failures
  in `test_settings_vnet_peering.py` / `test_terminal_exec.py`, no coupling to
  this change).
- `uv run ruff check api/services/blast/submit_gates.py api/routes/blast/submit.py api/tests/test_blast_submit_gates.py` — clean.
- `npx vitest run src/pages/blastSubmit/memoryFit.test.ts src/pages/blastSubmit/submitValidation.test.ts src/pages/blastSubmit/shardingAvailability.test.ts`
  — 28 passed.
- `cd web && npm run build` — type-check + bundle succeed.
