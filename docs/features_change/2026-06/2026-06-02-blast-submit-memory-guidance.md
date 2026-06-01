# BLAST submit: actionable hint when the database does not fit node memory

## Motivation

A dashboard BLAST submit against `core_nt` failed with `phase: submit_failed`
and a raw ElasticBLAST error:

```
ERROR: BLAST database https://.../blast-db/core_nt/core_nt memory requirements
exceed memory available on selected machine type "Standard_E16s_v5". Please
select machine type with at least 251.7GB available memory.
```

Root cause: the SPA sends the cluster's real workload node SKU as
`machine_type` (here `Standard_E16s_v5`, 128 GB) and defaults
`sharding_mode` to `off`. A full-database `core_nt` BLAST needs ~251.7 GB, so
ElasticBLAST's submit pre-flight rejects it. The dashboard cannot edit the
generated INI at submit time, and the surfaced error did not tell the user how
to recover — even though the supported recovery path (the "Sharded throughput"
execution profile, which partitions a prepared DB across nodes) already exists
in the UI.

## User-facing change

When ElasticBLAST rejects a submit because the full database does not fit the
selected node's memory, the dashboard now appends an actionable remediation
hint to the failure message:

> This database does not fit in the cluster node's memory for a full-database
> BLAST. Select the "Sharded throughput" execution profile to partition the
> database across nodes so each shard fits node memory, or recreate the cluster
> with a larger machine type, then resubmit.

The failure is otherwise unchanged (`status: failed`, `phase: submit_failed`).
The hint is only appended for this specific, non-retryable failure class;
unrelated errors are untouched.

## API / IaC diff summary

- `api/tasks/blast/cli_parsing.py`: new pure helper `_submit_failure_guidance()`
  + constant `INSUFFICIENT_MEMORY_GUIDANCE`, detecting ElasticBLAST's
  "memory requirements exceed memory available" pre-flight rejection. Both
  re-exported from `api.tasks.blast`.
- `api/tasks/blast/cli_parsing.py`: `_submit_failure_guidance()` also detects the
  sibling `mem-limit` pre-flight rejection (`Memory limit "…" exceeds memory
  available on the selected machine type …`) and returns `MEMORY_LIMIT_GUIDANCE`.
  ElasticBLAST raises this as the same opaque exit-code-1 `INPUT_ERROR`, and the
  dashboard's advanced options expose `mem_limit`, so it is reachable from the
  UI and belongs to the same machine-type/resource-mismatch class. Sharding does
  not help here, so its guidance steers to a lower limit or a larger node SKU.
  The full-DB sharding hint is checked first and the two messages never collide.
- `api/tasks/blast/submit_task.py`: the non-retryable submit failure path now
  appends the guidance (when matched) to the error stored in `submit_failed`
  state and returned to the caller.
- No IaC, route, or response-shape change. No new dependency.
- `docs/user-guide/api-reference.md`: the submit examples no longer use the
  misleading `resource_profile: "core_nt_safe"` value (it is free-form metadata
  echoed back on status and does **not** size compute or enable sharding). The
  examples now use the documented default `"standard"`, and a warning
  admonition explains that a full-database `core_nt` BLAST needs a large-memory
  node or the "Sharded throughput" profile.

## Validation evidence

- `uv run ruff check api/tasks/blast/cli_parsing.py api/tasks/blast/submit_task.py api/tasks/blast/__init__.py api/tests/test_blast_tasks.py` — clean.
- `uv run pytest -q api/tests/test_blast_tasks.py` — 126 passed, including the
  new `test_submit_failure_guidance_detects_insufficient_node_memory` and
  `test_submit_failure_guidance_is_none_for_unrelated_errors`.

## Follow-up (not in this change)

A proactive frontend guard could disable/annotate the "Baseline" and "Warmed
database" execution profiles and steer the user to "Sharded throughput" when
the selected database does not fit the cluster's node SKU. Deferred to keep
this change low-risk; the enriched backend error now self-documents the fix.
