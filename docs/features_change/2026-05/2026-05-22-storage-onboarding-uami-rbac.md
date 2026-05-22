# Storage onboarding grants the API managed identity Blob RBAC

## Motivation

A deployed dashboard could discover or select a Storage account from another ElasticBLAST workspace, but `/api/blast/databases` then returned `access_denied` because the API reads blob data with the dashboard's shared user-assigned managed identity, not the browser caller. The resource onboarding route only granted `Storage Blob Data Contributor` to the caller's Entra object id.

## User-facing change

When a Storage account is created or re-onboarded through the dashboard resource flow, the backend now grants `Storage Blob Data Contributor` to the shared dashboard managed identity as well as the signed-in caller. After RBAC propagation, database listing, uploads, downloads, terminal-side `azcopy`, and BLAST submit paths can use that Storage account through the API/worker/terminal sidecars.

If the backend cannot grant the shared identity Blob RBAC, Storage onboarding now fails immediately instead of reporting success and leaving the next database request to fail with `access_denied`. Deployed `access_denied` messages also point operators at the shared managed identity rather than the local `az login` identity.

## API / IaC diff summary

- `api.services.monitoring.ensure_storage_account()` now reads `SHARED_IDENTITY_PRINCIPAL_ID` and assigns Blob data-plane RBAC to that Service Principal id.
- The role assignment helper now uses the subscription-scoped built-in role definition id and accepts an explicit principal type.
- Shared-identity role assignment failures are treated as fatal for Storage onboarding; caller role assignment remains best-effort because the API data plane uses the shared identity.
- Storage data-plane `access_denied` remediation now distinguishes deployed Container App identity from local Azure CLI identity.
- `infra/modules/containerAppControl.bicep` injects `SHARED_IDENTITY_PRINCIPAL_ID` into the API, worker, and terminal sidecars.
- `infra/main.bicep` and `scripts/dev/postprovision.sh` pass the identity principal id into the Container App module.

## Validation evidence

- Targeted tests: `uv run pytest -q api/tests/test_monitoring_storage_rbac.py api/tests/test_storage_data.py`
- Lint: `uv run ruff check api/services/monitoring.py api/services/storage_data.py api/tests/test_monitoring_storage_rbac.py api/tests/test_storage_data.py`
- Shell/IaC syntax: `bash -n deploy.sh scripts/dev/postprovision.sh`
- Bicep build: `az bicep build --file infra/main.bicep --outfile /tmp/elb-dashboard-main.json`
