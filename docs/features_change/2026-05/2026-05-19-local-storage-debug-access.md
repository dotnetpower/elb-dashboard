# Local Storage Debug Access

## Motivation

Local debugging runs the api and worker from a developer machine, outside the
Container Apps private endpoint path. Workload Storage still defaults to
`publicNetworkAccess=Disabled`, so local DB, query, and result debugging needs a
safe, explicit way to open the Storage firewall to the caller IP without
weakening production posture.

## User-Facing Change

`local-run.sh` now exposes explicit Storage debug commands:

- `scripts/dev/local-run.sh storage-on`
- `scripts/dev/local-run.sh storage-status`
- `scripts/dev/local-run.sh storage-off`

Local backend processes also default `LOCAL_DEBUG_AUTO_OPEN_STORAGE=true`, so
routes that have full Storage ARM scope can best-effort open the account to the
caller IP before data-plane reads and writes. The Container App guard remains in
place; deployed environments refuse this path.

## API / IaC Diff Summary

- Added `storage-on`, `storage-off`, and `storage-status` local-run entrypoints
  that delegate to `scripts/dev/storage-public-access.sh` with
  `ELB_LOCAL_STORAGE_ACCOUNT` / `ELB_LOCAL_STORAGE_RG` defaults.
- Applied the local Storage access guard to BLAST result listing, file preview,
  analytics, downloads, exports, and DB order-oracle status writes when
  `subscription_id`, `resource_group`, and `storage_account` are present.
- Threaded `resource_group` through frontend result/file API calls so the
  backend has enough ARM scope to open the local debug window.
- Updated `.github/copilot-instructions.md` and `AGENTS.md` to document the
  explicit local-only Storage debug contract.
- No Bicep change. Production Storage remains private-only.

## Validation Evidence

- `bash -n scripts/dev/local-run.sh scripts/dev/storage-public-access.sh`
- `uv run pytest -q api/tests/test_storage_public_access.py api/tests/test_blast_results_routes.py`
- `cd web && npm run build`
