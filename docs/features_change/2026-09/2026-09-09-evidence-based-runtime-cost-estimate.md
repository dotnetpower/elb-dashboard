---
title: Evidence-based BLAST runtime and cost estimate
description: Show a non-blocking submit-page estimate only when comparable completed-job evidence is sufficient.
tags: [blast, ui]
---

# Evidence-based BLAST runtime and cost estimate

## Motivation

The submit page had a cost-estimate stub but no defensible runtime model.
Static program multipliers would produce precise-looking numbers without
evidence, especially across databases, query sizes, VM families, and node
counts that scale differently.

## User-facing change

The Runtime summary now requests an estimate only when inline FASTA, database
letter count, and a non-zero workload pool are known. It displays the robust
median duration, interquartile range, comparable sample count, confidence, and
approximate compute cost. Input changes are debounced by 750 ms and estimates
are cached for 60 seconds.

While changed input is still inside the debounce window, the previous input's
estimate is hidden and the summary reads **Calculating**. An estimate for one
query/cluster combination is never shown beside a newer form state.

Fewer than three comparable completed jobs returns an explicit
`insufficient_samples` state. No duration or cost is shown in that case. The
estimate is informational: it is not part of validation, preflight, submit
payloads, admission, budgets, or Service Bus messages.

## API change

`POST /api/blast/runtime-estimate` accepts a typed read-only request. The
service scans at most 500 existing rows in the selected scope and keeps only
terminal-success samples with matching program, normalized database name, and
workload VM SKU plus complete query/database size, node-count, and compute
duration evidence. Extreme instrumentation/unit errors beyond a fourfold median
band are excluded. Repository failure degrades to HTTP 200 with
`available=false`.

No historical row, cost preference, submit state, queue, or completion event is
written.

The pre-existing `/api/blast/cost-estimate` lab-tool stub is unchanged.

## Validation

- `uv run pytest -q api/tests/test_blast_runtime_estimate.py` - 5 passed.
- Runtime estimate plus existing cost route/estimator suites - 18 passed.
- Frontend runtime model/display tests - 5 passed.
- `npm --prefix web run build` - passed.
- Protected Service Bus source hashes remained identical to the pre-expansion
  baseline.