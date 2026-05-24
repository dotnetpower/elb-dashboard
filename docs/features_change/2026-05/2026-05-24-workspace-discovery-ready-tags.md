# Workspace Discovery Ready Tags

## Motivation

Numbered deployments can create `rg-elb-dashboard-01` side-by-side with an existing dashboard. The resource group previously received `elb-*` discovery tags during the Bicep resource-group creation step, so local dashboards could auto-select a partially provisioned workspace while `azd up` was still building images and swapping the Container App template.

## User-facing change

New deployments no longer expose a resource group as an auto-discoverable dashboard workspace until `postprovision.sh` finishes the image builds, applies the six-sidecar Container App template, and observes `/api/health` returning 200.

## API / IaC diff summary

- `infra/main.bicep` now applies only base azd/cost/topology tags to the resource group at creation time; child resources still receive the full workspace tags for tracing.
- `scripts/dev/postprovision.sh` now writes the `elb-workload-rg`, `elb-acr`, and `elb-storage` discovery tags only after the deployment health check succeeds.

## Validation evidence

- `az bicep build --file infra/main.bicep --outfile infra/main.json`
- `bash -n scripts/dev/postprovision.sh deploy.sh scripts/dev/resolve-resource-group.sh`
- Live tag repair after the issue was observed: `rg-elb-dashboard` now carries `elb-workload-rg`, `elb-acr`, and `elb-storage` discovery tags for the existing workspace, while `rg-elb-dashboard-01` has those `elb-*` discovery tags removed until a healthy deployment can mark it ready.