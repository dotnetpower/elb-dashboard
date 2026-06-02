# Build DB order oracle button gated on AKS cluster running

## Motivation

The "Build DB order oracle" button lives in the Storage Account card's BLAST
Databases modal. The Storage card is cluster-agnostic, so the button stayed
enabled whenever a database was downloaded — even when every AKS workload
cluster was stopped. The oracle build creates Kubernetes Jobs that run
`blastdbcmd` on the already-warmed cluster nodes, so clicking it against a
stopped cluster could only fail. Worse, the failure surfaced ~10 s late: the
backend walked into the node-local warmup/K8s calls and timed out instead of
rejecting fast.

This was the one real gap found in an audit of "buttons that require a running
cluster but stay enabled when all clusters are stopped". The other candidates
(warmup start/release, kubectl, BLAST submit, cluster power controls, PLS
OpenAPI deploy) already gate on cluster state.

## User-facing change

- The Build Oracle button is now disabled while the target AKS cluster
  (`elb-cluster`) is not Running, with the tooltip "AKS cluster is not running —
  start it before building the order oracle".
- If a user still reaches the endpoint while the cluster is stopped (stale UI,
  direct API call), the backend now returns a fast `409 aks_unavailable` with a
  "Start the cluster from the dashboard before building the order oracle"
  message instead of hanging ~10 s on a Kubernetes timeout.
- Both surfaces degrade open: when AKS status cannot be resolved (ARM
  unreachable, cluster not found in the list), the button stays enabled and the
  backend probe does not short-circuit, preserving the existing behaviour rather
  than locking a legitimate operator out on a transient hiccup.

## API / code diff summary

- `api/routes/blast/databases.py` — `blast_database_order_oracle` gains an
  ARM `get_cluster_health` precheck immediately after credential acquisition and
  before the Storage listing / K8s calls. Mirrors the precheck already in
  `/api/storage/prepare-db` (`mode=aks`). Raises `HTTPException(409, {"code":
  "aks_unavailable", ...})` when the cluster is provably not Running; degrades
  open when the probe raises.
- `web/src/components/cards/StorageCard.tsx` — derives `clusterReady` from the
  existing subscription-scoped AKS query (`isAksWorkloadReady` on the cluster
  matching the oracle target name; defaults true when unknown) and threads it
  to `BlastDbSection`.
- `web/src/components/cards/storage/BlastDbSection.tsx` /
  `BlastDbModal.tsx` / `BlastDbRow.tsx` — pass `clusterReady` through and fold
  it into `oracleDisabled`, with a dedicated `oracleDisabledReason` tooltip when
  the cluster is stopped.

## Validation evidence

- `uv run pytest -q api/tests/test_blast_oracle_aks_route.py` → `2 passed`
  (`test_oracle_returns_409_when_cluster_stopped`,
  `test_oracle_degrades_open_when_health_probe_raises`).
- `uv run ruff check api/routes/blast/databases.py
  api/tests/test_blast_oracle_aks_route.py` → "All checks passed!".
- `cd web && npm run build` → built successfully.
- `uv run pytest -q api/tests` → `2441 passed` (the single
  `test_openapi_hidden_by_default` flake is a pre-existing parallel
  test-isolation issue around docs-exposure env state; it passes in isolation
  and alongside the new test).
