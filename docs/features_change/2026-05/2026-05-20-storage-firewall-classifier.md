# Storage Firewall Classifier for Local Debug

## Motivation

Local development can fail to list BLAST database blobs even after the Storage
account is set to `publicNetworkAccess: Enabled` with `defaultAction: Deny` and
the caller IP in `ipRules`. Azure Blob data-plane returns the same broad
`AuthorizationFailure` shape for selected-network firewall rejects, so the UI
was falling through to the RBAC-only `access_denied` message and hiding the
local-debug recovery path.

## User-facing change

`GET /api/blast/databases` now returns `degraded_reason: "firewall_blocked"`
when ARM reports `publicNetworkAccess: Enabled`, `defaultAction: Deny`, and the
data plane still rejects the local request with `AuthorizationFailure`. The
BLAST Databases section treats this as a local-debug network block, keeps the
`Enable for local debug` action visible, and uses copy that says the selected
network firewall is still blocking the host instead of saying the account is
private-only or RBAC-only.

## API / IaC Diff Summary

- `api/services/storage_data.py`: inspect Storage `network_rule_set` in
  `classify_storage_failure` and return `firewall_blocked` for selected-network
  rejects.
- `web/src/components/cards/storage/useBlastDb.ts`: treat `firewall_blocked` as
  local-debug recoverable network access.
- `web/src/components/cards/storage/BlastDbSection.tsx` and
  `BlastDbModal.tsx`: render reason-specific blocked-state copy.
- No IaC changes.

## Validation Evidence

- `uv run pytest -q api/tests/test_storage_data.py api/tests/test_storage_public_access.py`
  -> 39 passed.
- `uv run ruff check api/services/storage_data.py api/tests/test_storage_data.py`
  -> all checks passed.
- `cd web && npm run build` -> TypeScript and Vite production build succeeded.
- Live local API check against `elbstg01` returned:

  ```json
  {
    "databases": 0,
    "degraded": true,
    "degraded_reason": "firewall_blocked",
    "public_access_disabled": false,
    "local_debug_access_blocked": true,
    "caller_ip": "61.80.8.142",
    "caller_ip_in_rules": true
  }
  ```

- Browser snapshot on the Dashboard confirmed the BLAST Databases section shows
  `Storage firewall is still blocking this host` plus the `Enable for local
  debug` action.