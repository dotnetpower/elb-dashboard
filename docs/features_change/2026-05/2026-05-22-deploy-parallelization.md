# Deploy Script Parallelization

## Motivation

Full [`azd up`](https://learn.microsoft.com/azure/developer/azure-developer-cli/overview) deployments were spending avoidable time on repeated [Azure resource provider](https://learn.microsoft.com/azure/azure-resource-manager/management/resource-providers-and-types) checks and on serial terminal base-image preparation before unrelated application image builds could start.

## User-Facing Change

`./deploy.sh` now performs the provider check once for the wrapper-driven deployment and passes a sentinel into the `azd` hooks so the preprovision and postprovision hooks skip duplicate provider refreshes in the same run. Direct `azd up` still runs provider validation normally.

Provider registration now checks independent providers with bounded parallelism. The postprovision image phase starts the API and frontend builds immediately while the terminal image job waits for its content-hashed base image before building the runtime overlay.

`./deploy.sh` also prints an upfront estimate that full deployments usually take 10-20 minutes and points code-only changes to `scripts/dev/quick-deploy.sh`.

## API/IaC Diff Summary

- No API route or Bicep resource contract changes.
- `scripts/dev/register-providers.sh` adds `PROVIDER_REGISTRATION_CONCURRENCY` with a default of `4` and preserves hard-fail behavior for deployment providers.
- `deploy.sh`, `azure.yaml`, and `scripts/dev/postprovision.sh` share `ELB_PROVIDER_REGISTRATION_READY` for duplicate-check suppression inside wrapper-driven deployments.
- `scripts/dev/postprovision.sh` keeps the single six-sidecar Bicep swap intact and only changes image-build scheduling before that swap.

## Validation Evidence

- `bash -n deploy.sh scripts/dev/register-providers.sh scripts/dev/postprovision.sh scripts/dev/acr-build-access.sh scripts/dev/terminal-base-image.sh`
- `PROVIDER_REGISTRATION_CONCURRENCY=4 PROVIDER_REGISTRATION_TIMEOUT_SECONDS=120 bash scripts/dev/register-providers.sh --subscription <active-subscription>`
