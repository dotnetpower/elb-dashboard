# Synchronous BLAST submit preflight gates

## Motivation

`POST /api/blast/submit` accepted requests even when the execution pipeline was
guaranteed to fail (terminal sidecar down, `EXEC_TOKEN` unset, AKS cluster not
running, selected database missing in Storage, broker offline). The submit task
would then queue, run, and eventually surface a degraded phase such as
`terminal_unavailable` or `database_unavailable` — a slow, opaque failure mode.
Per the navigation discussion ("if terminal is abnormal, execution should be
blocked"), the dashboard now fails closed up front instead of writing a
stranded `queued` row.

## User-facing change

A `POST /api/blast/submit` that violates any critical precondition now returns
HTTP `409 Conflict` with a structured body that names every blocking gate and a
remediation action, e.g.:

```json
{
  "code": "blocked_by_preflight",
  "message": "AKS cluster 'elb-cluster' is Stopped. Start it first.",
  "blocking_gates": [
    {
      "id": "aks_cluster",
      "status": "fail",
      "severity": "critical",
      "error_code": "cluster_not_ready",
      "message": "AKS cluster 'elb-cluster' is Stopped. Start it first.",
      "action": "Start cluster",
      "action_type": "start_cluster"
    }
  ],
  "gates": [ ... full report ... ]
}
```

The gate evaluator caches AKS/Storage probes for 5 seconds per process so a
retry burst from a single click does not amplify ARM traffic. Local sidecar
checks (terminal sidecar `/healthz`, `EXEC_TOKEN` presence, broker ping) are
re-checked on every submit.

Callers that want to ignore "unknown" gates (e.g. ARM throttled) can opt in
with the `X-Submit-Allow-Unverified: true` request header. Definitive failures
still block.

## API / IaC diff summary

* New service module `api/services/blast/submit_gates.py` with
  `evaluate_submit_gates(...)` returning a `SubmitGatesReport`. Gates:
  `exec_token`, `terminal_sidecar`, `broker`, `aks_cluster`, `blast_database`.
* `api/routes/blast/submit.py` now calls the evaluator after contract
  validation and raises HTTPException 409 when any critical gate blocks.
* `api/routes/blast/preflight.py` (`POST /api/blast/pre-flight`) now also
  emits the new `terminal_sidecar` and `exec_token` rows in `checks[]` so the
  SPA's existing `BlastSubmitFooter` hard-disables the Run BLAST button when
  either local sidecar gate is failing (the existing `preFlightBlocked` logic
  already disables on `ready === false`).
* Legacy flat alias `api/services/blast_submit_gates.py` added so the facade
  contract test stays green.
* No IaC change.

## Validation evidence

* `uv run pytest -q api/tests/test_blast_submit_gates.py` — 16 passed (new file).
* `uv run pytest -q api/tests` — 1499 passed (full backend suite).
* `uv run ruff check api` — clean.
* `cd web && npm run build` — clean (no UI code change required; the existing
  preflight panel surfaces the new gates).
