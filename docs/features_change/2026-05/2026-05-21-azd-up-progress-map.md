# azd up progress map

## Motivation

Fresh deployments spend time in provider registration, Bicep provisioning, image builds, Container App template swap, and health checks. The default azd output made it hard to tell which phase was active or how much of the deployment remained.

## User-facing change

- `azd up` now prints an `azd up progress map` before long-running work starts.
- Preprovision prints `[1/7] Provider registration` and hands off to `[2/7] Bicep provision`.
- Postprovision marks `[3/7] App registration`, `[4/7] Resource validation`, `[5/7] Image builds`, `[6/7] Sidecar swap`, and `[7/7] Health check`.
- Provider registration now shows per-provider counters within deployment and first-run workflow provider groups.

## API / IaC diff summary

- Added `scripts/dev/azd-progress.sh` as the shared progress marker helper.
- Updated `azure.yaml` hooks to stream preprovision progress and print the stage map for raw `azd up`.
- Updated `deploy.sh`, `scripts/dev/postprovision.sh`, and `scripts/dev/register-providers.sh` to use clear numbered progress output.

## Validation evidence

- `bash -n scripts/dev/azd-progress.sh scripts/dev/register-providers.sh deploy.sh scripts/dev/postprovision.sh`
- `./scripts/dev/azd-progress.sh plan`
- `./scripts/dev/azd-progress.sh step 5 "Image builds" "sample detail"`
- `./scripts/dev/azd-progress.sh done 5 "Image builds"`
- `./scripts/dev/register-providers.sh --subscription 577d6332-de48-4a30-be66-dded26a712ea` -> printed deployment provider counters `[1/10]` through `[10/10]` and workflow counters `[1/3]` through `[3/3]`.
- `azd provision --preview --no-prompt` -> generated the infra preview successfully; preview mode does not run/show hooks, so hook output was validated with the helper and shell checks above.