# Lean azd deployment and workspace tag discovery

## Motivation

Fresh `azd up` deployments should keep the baseline observability footprint lean by creating Log Analytics only, while still letting the dashboard immediately discover the workload resource group and related platform resources.

## User-facing change

- `deploy.sh` now persists `ENABLE_APPLICATION_INSIGHTS=false` by default so the first deployment does not create Application Insights unless explicitly requested.
- `postprovision.sh` merges dashboard discovery tags onto the platform resource group after provision, including workload RG, ACR RG, ACR name, Storage account name, and region.
- The setup wizard auto-fills Workload RG, ACR RG, ACR, Storage, and region from `elb-*` resource group tags when available.

## API / IaC diff summary

- `infra/main.bicep` and regenerated `infra/main.json` keep Application Insights behind `enableApplicationInsights` and apply workspace tags to the platform RG and modules.
- `infra/main.parameters.json` keeps `ENABLE_APPLICATION_INSIGHTS=false` as the default azd parameter value.
- `scripts/dev/postprovision.sh` backfills workspace tags for existing environments so the dashboard can discover them without requiring a full redeploy.

## Validation evidence

- `az bicep build --file infra/main.bicep --outfile infra/main.json`
- `bash -n deploy.sh scripts/dev/postprovision.sh`
- `cd web && npm run build`
- Live tag backfill verified on `rg-elb-dashboard`: `elb-workload-rg=rg-elb-dashboard`, `elb-acr-rg=rg-elb-dashboard`, `elb-storage=stelbxb36pe344x4he`, `elb-acr=acrelbxb36pe344x4he`, `elb-region=eastus`.