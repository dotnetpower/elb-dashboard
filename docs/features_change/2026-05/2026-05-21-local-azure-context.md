# Local Azure Context Guard

## Motivation

Local dashboard sessions could keep using stale Azure credential state from a different tenant. The symptom looked like a network or database issue because monitor routes degraded after ARM, Storage, ACR, and AKS calls returned `InvalidAuthenticationTokenTenant`.

## User-facing change

`scripts/dev/local-run.sh` now loads the safe Azure context keys from the root `.env` file and validates the current `az account` subscription and tenant before starting local services. The local web dev server also receives `VITE_AZURE_TENANT_ID` from the same source so ignored `web/.env.local` values cannot silently point MSAL at a different tenant.

## API/IaC diff summary

- Local service startup now imports `AZURE_SUBSCRIPTION_ID`, `AZURE_TENANT_ID`, `ELB_LOCAL_STORAGE_ACCOUNT`, and `ELB_LOCAL_STORAGE_RG` from `.env` when they are not already exported.
- Local startup fails fast when `az account show` does not match the expected subscription or tenant.
- The backend credential singleton uses `AzureCliCredential(tenant_id=AZURE_TENANT_ID)` in local non-managed-identity environments, preventing stale Azure Developer CLI credentials from another tenant from satisfying SDK requests.
- No IaC changes.

## Validation evidence

- `uv run pytest -q api/tests/test_auth_caching.py`
- `bash -n scripts/dev/local-run.sh`
- Manual smoke: `/api/monitor/aks` returned the AKS cluster without `degraded` after restarting local API/worker with the corrected Azure context.