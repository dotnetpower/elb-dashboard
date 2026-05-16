# Local-debug auto-open for Storage publicNetworkAccess (prepare-db)

## Motivation

`POST /api/storage/prepare-db` triggers a server-side copy from NCBI's
public S3 bucket into the workload Storage account's `blast-db` container.
In production the api sidecar reaches that account over the platform VNet
private endpoint, so `publicNetworkAccess: Disabled` (project policy §9)
is invisible to it.

When the api is started from a developer laptop, however, every blob call
hits the *public* data plane and fails because the account refuses public
traffic. The result is that **DB downloads cannot be exercised locally** —
the SPA reports the request as scheduled but no copy ever starts.

The existing escape hatch (`scripts/dev/storage-public-access.sh on`) is a
manual two-step the user has to remember every session. The user asked for
this to happen automatically when, and **only when**, running locally.

## User-facing change

If the developer opts in by setting

```bash
LOCAL_DEBUG_AUTO_OPEN_STORAGE=true
```

…on the api process, `prepare_db` now:

1. Looks up the Storage account's current `publicNetworkAccess` /
   `networkRuleSet` via ARM.
2. If the caller's public IPv4 (resolved via api.ipify.org) is not already
   in `ipRules` with `defaultAction=Deny`, applies a single
   `StorageAccount.update` that sets
   `publicNetworkAccess=Enabled, defaultAction=Deny,
   bypass=AzureServices, ipRules=[<existing…>, <caller>]`.
3. Logs a `WARNING` containing the action, account, IP and a reminder to
   close the window with `scripts/dev/storage-public-access.sh off`.
4. Returns the standard `prepare_db` payload extended with a
   `local_debug_storage_opened` field and a closing instruction in
   `output`.

The flow then proceeds with the normal `start_copy_from_url` per file.

If `LOCAL_DEBUG_AUTO_OPEN_STORAGE` is unset, or the env var
`CONTAINER_APP_NAME` is present (i.e. we are inside a Container App
revision), the helper is a strict no-op. There is no other entry point.

## API / IaC diff summary

### `api/services/storage_public_access.py` (new)

* `is_local_debug_auto_open_enabled()` — gate combining explicit env
  opt-in (`LOCAL_DEBUG_AUTO_OPEN_STORAGE`) and Container App detection
  (`CONTAINER_APP_NAME`). The Container App check is the load-bearing
  operational guard; the deployed control plane can never auto-open
  Storage even if the opt-in env leaks into a manifest.
* `ensure_local_storage_access(credential, sub, rg, account)` —
  idempotent, never raises. Returns one of `noop | already_open |
  ip_added | opened | failed`. Re-uses
  `api.services.azure_clients.storage_client` and the
  `azure-mgmt-storage` `NetworkRuleSet` / `IPRule` /
  `StorageAccountUpdateParameters` models. Caller IP is resolved with
  `httpx → api.ipify.org` and validated as a bare IPv4 (matching
  the constraint Storage `ipRules` already enforce — see
  [scripts/dev/storage-public-access.sh](../../../scripts/dev/storage-public-access.sh)).
* **Auto-close is intentionally not implemented.** The background copy
  thread keeps the account in active use after the HTTP request returns,
  and per project policy §9 the close action stays manual (the script).

### `api/routes/storage.py`

* `prepare_db` calls `ensure_local_storage_access` immediately after
  request validation and before the NCBI lookup. The result feeds into
  the existing structured log line and, when an `opened` / `ip_added`
  flip happened, is surfaced in the response so the SPA / curl caller
  sees the temporary network change and the close-hint.

### `api/tests/test_storage_public_access.py` (new)

15 unit tests covering the gate (default-off, opt-in, Container App
override, falsy values), the noop branches, the three side-effect
branches (`already_open`, `opened`, `ip_added`) with exact assertions on
the `StorageAccount.update` payload, and the two failure modes (ARM read
error, caller IP unresolvable).

## Validation evidence

```
$ uv run pytest -q api/tests/test_storage_public_access.py
15 passed in 0.54s

$ uv run pytest -q api/tests
91 passed in 9.78s

$ uv run ruff check api/services/storage_public_access.py api/routes/storage.py
All checks passed!
```

## Operational notes

* The opt-in env is **not** added to any deployment manifest, Bicep
  module, Container App template, or compose file. It only ever gets set
  manually on a developer shell next to `AUTH_DEV_BYPASS=true`.
* Even with the opt-in, the `CONTAINER_APP_NAME` guard means a misconfig
  inside the Container App still results in a clean no-op rather than
  Storage being opened in production.
* Closing the network window remains a manual step
  ([scripts/dev/storage-public-access.sh](../../../scripts/dev/storage-public-access.sh)
  `off`) — the helper logs and the API response both repeat that hint
  every time it acts.
