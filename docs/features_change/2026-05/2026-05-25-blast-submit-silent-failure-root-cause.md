# 2026-05-25 — BLAST submit silently failed (kubeconfig drift + storage env mismatch)

## Motivation

A user reported that a BLAST submission appeared to run with no error but never
produced a job: the dashboard did not show the job row, the worker exit code
was 0, and the SPA's job poll returned 404.

Investigation revealed three concurrent latent faults that combined into a
single silent failure:

1. **Stale host kubeconfig.** The previous AKS cluster (`aks-elb-e2e-core-nt`)
   had been deleted earlier in the day, but `~/.kube/config`'s default context
   still pointed at it. The `terminal` sidecar / local terminal-exec inherits
   that context, so `kubectl` (invoked transitively by
   `elastic-blast submit`) failed with
   `Unable to connect to the server: dial tcp: lookup aks-elb-e2e-core-nt-*.hcp.eastus2.azmk8s.io ... no such host`.
   Upstream `elastic-blast` did not surface this as a non-zero exit — the
   Celery task happily reported `phase=submitted, exit_code=0`.

2. **Backend env pointed at a different storage account than the SPA.** A
   fresh `azd up` had produced a sibling environment
   (`rg-elb-dashboard-01` / `stelbdashboard01mul5oh5j`), and `azd env` /
   `local-run.sh` derived `AZURE_TABLE_ENDPOINT` from that. The SPA's
   workspace config, however, still anchored on the previous account
   (`rg-elb-dashboard` / `stelbdashboardtest01`). The caller had RBAC
   only on the older account, so the very first hop of the submit pipeline
   — `JobStateRepository.create()` writing the `jobstate` row — hit a 403
   `AuthorizationFailure` against the new account. That 403 was swallowed
   by the broad `try/except` in `api/routes/blast/submit.py` ("failed to
   create job state: ..." warning only), the SPA still received a job id,
   and every subsequent `_update_state` call against the missing row raised
   `KeyError(job_id)` which was *also* swallowed by `api/tasks/blast/state.py`.

3. **`elastic-blast submit` does not refresh kubeconfig per invocation.**
   Even when the cluster identifiers in the dashboard's request are correct,
   the toolchain trusts the current kubeconfig default context. There is no
   guard that the active context matches the requested cluster.

The user-visible result was: submit "succeeded" silently, no Kubernetes job
was ever created, the dashboard had nothing to show.

## User-facing change

- Future BLAST submits refresh the terminal sidecar's kubeconfig for the
  requested AKS cluster before invoking `elastic-blast`. If `az aks
  get-credentials` fails (cluster gone, RBAC missing, transient ARM error)
  the submit task fails fast with phase `terminal_kubeconfig_failed` and a
  human-readable error code rather than silently exiting 0.
- The local backend env was repinned at the live workspace
  (`stelbdashboardtest01` / `rg-elb-dashboard`) so jobstate Table writes
  succeed. The unused sibling deployment (`-01` suffix) was left in place
  but is no longer the default target.
- Host `~/.kube/config` was refreshed once via
  `az aks get-credentials --resource-group rg-elb-cluster --name elb-cluster-01 --overwrite-existing`
  so the currently running submit and any short-term retries hit the live
  cluster. A `.kube/config.bak.<epoch>` backup was kept.

## API / IaC diff summary

Backend:

- `api/tasks/blast/submit_runtime.py`
  - New `TerminalKubeconfigError` exception.
  - New `_ensure_terminal_kubeconfig_context(terminal_run, *, subscription_id, resource_group, cluster_name)` helper that runs
    `az aks get-credentials --overwrite-existing --only-show-errors` via
    `terminal_run` with a 90s timeout. No-op when any identifier is blank.
- `api/tasks/blast/__init__.py` re-exports `TerminalKubeconfigError` and
  `_ensure_terminal_kubeconfig_context` (preserves the facade contract that
  tests assert against).
- `api/tasks/blast/submit_task.py` calls the new helper between
  `_ensure_terminal_azure_cli_login` and `_stream_submit_command`, and adds a
  matching `except _blast.TerminalKubeconfigError` branch that funnels into
  `_retry_or_fail` with error code `terminal_kubeconfig_failed` and phase
  `terminal_kubeconfig_failed`.

Tests:

- `api/tests/test_blast_tasks.py`
  - `test_ensure_terminal_kubeconfig_context_runs_get_credentials`
  - `test_ensure_terminal_kubeconfig_context_raises_on_failure`
  - `test_ensure_terminal_kubeconfig_context_skips_when_identifiers_missing`
  - `_ensure_terminal_kubeconfig_context` and `TerminalKubeconfigError`
    added to the `required` symbol set in the facade contract guard.

Local environment:

- `.env` now sets `ELB_LOCAL_STORAGE_ACCOUNT=stelbdashboardtest01` and
  `ELB_LOCAL_STORAGE_RG=rg-elb-dashboard`, which `scripts/dev/local-run.sh`
  resolves into `AZURE_TABLE_ENDPOINT` / `AZURE_BLOB_ENDPOINT` for api +
  worker + beat. The pre-edit file is preserved at `.env.bak.<epoch>`.

No IaC change.

## Race-condition note

`~/.kube/config` is shared per terminal sidecar (and per host in the local
dev loop). The per-(cluster, namespace) submit lock prevents same-cluster
races; unrelated cross-cluster submits issued in the same ~second could
in principle interleave. This is an acceptable trade-off for the dashboard
which is single-tenant and rarely runs more than one cross-cluster submit
concurrently. The longer-term fix is to plumb a per-job `--kubeconfig`
through `elastic-blast` so submits do not share state.

## Validation

- `uv run ruff check api/tasks/blast/submit_runtime.py api/tasks/blast/submit_task.py api/tasks/blast/__init__.py` → `All checks passed!`
- `uv run pytest -q api/tests/test_blast_tasks.py` → `121 passed` (was 118; +3 new)
- `uv run pytest -q api/tests` → `1460 passed`
- Live env probe after the env repin:
  - `tr '\0' '\n' < /proc/<api-pid>/environ | grep AZURE_TABLE_ENDPOINT`
    → `https://stelbdashboardtest01.table.core.windows.net`
  - `curl http://127.0.0.1:8085/api/health` → `{"status":"ok",...}`
  - Direct credential probe with `azure-data-tables` + `DefaultAzureCredential`
    successfully created and deleted a probe row in
    `stelbdashboardtest01/jobstate` (the exact write that was 403'ing
    before the repin).
- `az aks get-credentials --resource-group rg-elb-cluster --name elb-cluster-01 --overwrite-existing`
  then `kubectl get nodes` → 9 blastpool nodes Ready.
