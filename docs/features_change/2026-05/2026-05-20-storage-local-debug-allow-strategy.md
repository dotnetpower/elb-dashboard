# Storage local-debug: switch from Deny+ipRule to Allow strategy

**Date**: 2026-05-20  
**Scope**: `api/services/storage_public_access.py`, `scripts/dev/storage-public-access.sh`, `api/tests/test_storage_public_access.py`

---

## Motivation

`/api/blast/databases` was returning `firewall_blocked` degraded state, causing the DB list to show 0 items.

Root cause confirmed after live debugging:

| Strategy | Config | Result after propagation |
|---|---|---|
| `defaultAction=Deny + ipRule[61.80.8.142]` | 24h+ | HTTP 403 (never works) |
| `defaultAction=Deny + ipRule[61.80.8.142]` | 2 min fresh | HTTP 403 (still broken) |
| `defaultAction=Allow` | 90 s | HTTP 200 ✓ |

For ADLS Gen2 (`isHnsEnabled: true`) accounts with an approved private endpoint in the AKS VNet, **`defaultAction=Deny + ipRule` does not reliably propagate to the data plane** — even after 24+ hours. `defaultAction=Allow` is the only reliable mechanism for developer laptop access.

`allowSharedKeyAccess: false` remains in effect, so Azure AD authentication is still enforced at every data-plane request. The change only affects network-layer access control.

The previous `ensure_local_storage_access` code:
1. Detected caller's public IP via `api.ipify.org`
2. Set `defaultAction=Deny + ipRules=[callerIP]`
3. Returned `already_open` if the IP was already in the rules (even though it was still blocked)

This combination silently masked the issue — ARM state said "open" but data plane was blocked.

---

## User-facing change

- `GET /api/blast/databases` now returns the full database list instead of the `firewall_blocked` degraded state when `LOCAL_DEBUG_AUTO_OPEN_STORAGE=true` and the storage account is reachable.
- `scripts/dev/storage-public-access.sh on` sets `defaultAction=Allow` (not `Deny+ipRule`) and waits 90 s for propagation.
- `--ip` flag to `storage-public-access.sh on` is ignored (no longer needed, kept for CLI compatibility).

---

## Code diff summary

### `api/services/storage_public_access.py`

**`ensure_local_storage_access`**:
- `already_ok` condition: `Enabled + defaultAction==Allow` (was: `Enabled + Deny + callerIP in rules`)
- ARM update now sets `defaultAction=Allow`, no `ip_rules` (was: `Deny + [callerIP]`)
- `caller_ip` detection moved to after the update (informational only, `None` no longer blocks)
- Removed `IPRule` import (unused)
- Removed `existing_ips` collection (unused with Allow strategy)
- Docstring updated to describe new strategy and rationale

### `scripts/dev/storage-public-access.sh`

**`on` case**:
- Removed IP detection, validation, and `network-rule add/remove` steps
- `--default-action Allow` (was `Deny`)
- Propagation wait bumped from 10 s → 90 s to match observed propagation time
- Header comment updated with rationale for `Allow` strategy

### `api/tests/test_storage_public_access.py`

- `_make_account` helper: accepts explicit `default_action` parameter (was inferred from `ip_rules` length)
- `test_ensure_already_open`: asserts `defaultAction=Allow` as open condition, no IP check
- `test_ensure_opens_when_disabled`: asserts `defaultAction=Allow` in update params, no `ip_rules`
- `test_ensure_appends_caller_ip_when_partially_open` → renamed `test_ensure_updates_to_allow_when_enabled_with_deny`
- `test_ensure_returns_failed_when_caller_ip_unknown` → renamed `test_ensure_opened_when_caller_ip_unknown` (IP unknown no longer blocks)
- `test_ensure_already_open_is_cached`: updated mock account to `Allow`, removed `_detect_caller_ip` mock

---

## Validation evidence

```
# 1. Unit tests
uv run pytest -q api/tests/test_storage_public_access.py
# → 16 passed in 1.93s ✓

# 2. Storage set to Allow, 90s propagation, data-plane test
az storage account update -n elbstg01 -g rg-elb-01 --default-action Allow --bypass AzureServices
# HTTP:200 after 90s ✓

# 3. API smoke test
curl "http://127.0.0.1:8085/api/blast/databases?subscription_id=...&storage_account=elbstg01&resource_group=rg-elb-01"
# → {"databases": [{"name": "16S_ribosomal_RNA", ...}, {"name": "18S_fungal_sequences", ...},
#    {"name": "ITS_RefSeq_Fungi", ...}, {"name": "core_nt", ...}, {"name": "elb_compare_tiny", ...}]}
# 5 databases returned, no firewall_blocked ✓
```
