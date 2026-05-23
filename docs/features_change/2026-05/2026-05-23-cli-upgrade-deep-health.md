# cli-upgrade.sh — deep readiness probe + Storage isolation parity preflight

## Motivation
On 2026-05-23 the test Container App spent ~37 minutes silently failing
every `auto-warmup-reconcile` Celery beat tick because workload Storage
was set to `publicNetworkAccess=Disabled` while no Private Endpoint
existed for it (Bicep `lockdownPrivateNetworking=false` skipped the PE
branch). The api / worker / beat sidecars booted up healthy, `/api/health`
returned 200, but every Storage Table call returned the misleading
`403 AuthorizationFailure` — which is Azure Storage's response when the
public endpoint is hit while public access is disabled, indistinguishable
in the error code from RBAC denial.

The CLI deploy envelope did not catch this. `cli-upgrade.sh` only polled
the cheap `/api/health` liveness endpoint and called it a success, so
the silent failure mode could persist through a full `full --allow-dirty`
rolling update without any signal.

## User-facing change
Three improvements that compose into one safety envelope:

1. **Deeper post-deploy gate.** `cli-upgrade.sh` now polls
   `/api/health/ready` (readiness), which checks Redis + Azure
   credential + terminal sidecar + **Storage Table data plane**.
   On a 503 it dumps up to 5 KB of the JSON response body to stderr
   so the operator sees `azure_storage: { status: down, error: ... }`
   immediately, before auto-rollback fires.

2. **Pre-deploy parity check.** Before the snapshot stage,
   `cli-upgrade.sh` queries the workload Storage account and refuses
   the deploy when `publicNetworkAccess=Disabled` **and** zero Private
   Endpoints reference the account. The error message offers two
   recovery paths (quick `storage-public-access.sh on` vs proper
   `azd env set LOCKDOWN_PRIVATE_NETWORKING true && azd provision`)
   plus an explicit `--skip-parity-check` override.

3. **Readiness component coverage.** `/api/health/ready` gained a
   4th component, `azure_storage`, that performs the cheapest possible
   Table data-plane call (`list_tables(results_per_page=1)`) with 3 s
   connect / 3 s read timeouts. `AZURE_TABLE_ENDPOINT` unset → reports
   `skipped`; reachable → `ok`; raises → `down` + overall 503.

## API / IaC diff
* `api/routes/health.py::readiness`
  * Add `azure_storage` component check after the existing 3
  * 3 s timeouts via `connection_timeout` / `read_timeout` so a slow
    Storage cannot tarpit unauthenticated readiness callers
  * Liveness `/api/health` untouched (Container Apps platform probes
    still use it)
* `api/tests/test_smoke.py`
  * 3 new cases: `skipped`, `ok`, `down` paths via monkeypatched
    `TableServiceClient` (no real Azure calls in unit tests)
* `scripts/dev/cli-upgrade.sh`
  * `poll_health()` URL → `/api/health/ready`
  * Capture last response body (5 KB cap) and `cat >&2` on non-200
  * Help / plan-summary / rollback-failure strings updated to match
  * New `preflight_storage_parity()` between az-login preflight and
    snapshot; new `--skip-parity-check` flag + `--help` documentation
* `docs/operate/cli-upgrade.md`
  * Preflight checklist row for the Storage parity check
  * Health-check budget section rewritten for `/health/ready`
  * Mermaid flowchart updated to show `/api/health/ready`
  * Two new rows under Common failure modes covering preflight
    rejection and `azure_storage: down` 503 cases

No Bicep or Container App template changes; this is pure CLI envelope
plus a readiness-endpoint hardening.

## Validation
* `uv run pytest -q api/tests/test_smoke.py api/tests/test_route_contracts.py`
  → 84 passed (3 new readiness storage cases included).
* `uv run ruff check api/routes/health.py api/tests/test_smoke.py`
  → All checks passed.
* `bash -n scripts/dev/cli-upgrade.sh` → syntax OK.
* End-to-end reproduction against deployed `ca-elb-dashboard`:
  * `cli-upgrade.sh full --allow-dirty --dry-run` while Storage was
    `Enabled` → parity check passes, plan prints
    `Health: .../api/health/ready`.
  * `storage-public-access.sh off ...` → simulates the broken state.
  * `cli-upgrade.sh full --allow-dirty --dry-run` now exits 1 with the
    parity error message and recovery options.
  * `cli-upgrade.sh full --allow-dirty --dry-run --skip-parity-check`
    skips with a `WARN:` and proceeds to plan/dry-run.
  * `storage-public-access.sh on ...` restores connectivity.
* Confirmed worker `reconcile_auto_warmup` succeeds again after recovery
  (`Task ... succeeded in 0.087s: {status: completed}`).

## Operator note
On `ca-elb-dashboard` (test) workload Storage stays
`publicNetworkAccess=Enabled, defaultAction=Allow` while Bicep is
deployed with `lockdownPrivateNetworking=false`. Do not run
`storage-public-access.sh off` against this app until the Bicep is
re-deployed with `LOCKDOWN_PRIVATE_NETWORKING=true` so the
blob/dfs/table Private Endpoints actually exist.
