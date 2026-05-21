# Azure resource name convention

## Motivation

The Container Apps deployment still used older `control`-oriented names such as `ca-elb-control`, `id-elb-control-*`, `acrelb*`, and `stelb*`. New deployments should make the product identity obvious in Azure by using `elb-dashboard` where hyphens are allowed and `elbdashboard` where Azure requires compact alphanumeric names.

## User-facing change

- The bundled Container App is now named `ca-elb-dashboard`.
- Hyphen-friendly resources use the `elb-dashboard` prefix, including `rg-elb-dashboard`, `vnet-elb-dashboard`, `id-elb-dashboard-*`, `cae-elb-dashboard-*`, and `kv-elb-dashboard-*`.
- Compact global names use `elbdashboard`, including `acrelbdashboard*` and `stelbdashboard*`.
- Dashboard sidecar labels, diagnostics, setup docs, auth docs, and helper scripts now reference the new names.

## API / IaC diff summary

- `infra/main.bicep` centralizes the naming with `hyphenatedNamePrefix = 'elb-dashboard'` and `compactNamePrefix = 'elbdashboard'`.
- `infra/main.json` was regenerated from Bicep.
- Operational scripts and diagnostics now default to `ca-elb-dashboard` and `id-elb-dashboard-*`.

## Validation evidence

- `az bicep build --file infra/main.bicep --outfile infra/main.json`
- `bash -n deploy.sh scripts/dev/postprovision.sh scripts/dev/aks-equivalence-runner.sh scripts/dev/quick-deploy.sh scripts/dev/grant-local-rbac.sh`
- `uv run pytest -q api/tests/test_sidecar_metrics.py api/tests/test_storage_public_access.py api/tests/test_smoke.py` -> 91 passed.
- `uv run ruff check api`
- `cd web && npm run build`
- `azd provision --preview --no-prompt` showed `Create` for `ca-elb-dashboard`, `cae-elb-dashboard-xb36pe34`, `acrelbdashboardxb36pe344x`, `stelbdashboardxb36pe344x`, and `kv-elb-dashboard-xb36pe3` in the current environment.