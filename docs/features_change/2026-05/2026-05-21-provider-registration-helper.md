# One-command azd Bootstrap

## Motivation

Direct Container App operations can fail in a fresh subscription with `Subscription ... is not registered for the Microsoft.App resource provider` before the normal `azd up` preprovision hook has a chance to run. The initial deployment guide also required a manual App Registration step and a copied `API_CLIENT_ID`, which made a fresh clone feel harder than necessary.

## User-facing change

After Azure sign-in and azd environment selection, `azd up` can create or reuse the App Registration automatically, register required Azure resource providers, and deploy the full six-sidecar Container App without a manual `API_CLIENT_ID` copy step. Fresh clones can also run `./deploy.sh` to perform the login check, default azd environment setup, `azd up`, and browser launch in one command.

## API / IaC diff summary

- Added `scripts/dev/register-providers.sh` with the deployment provider list: `Microsoft.App`, `Microsoft.Authorization`, `Microsoft.ContainerRegistry`, `Microsoft.Insights`, `Microsoft.KeyVault`, `Microsoft.ManagedIdentity`, `Microsoft.Network`, `Microsoft.OperationalInsights`, `Microsoft.Resources`, and `Microsoft.Storage`.
- The same helper now starts first-run workflow provider registration for `Microsoft.Compute`, `Microsoft.ContainerService`, and `Microsoft.Quota`, covering VM family quota checks and dashboard-driven AKS provisioning before those flows are used.
- Updated `azure.yaml` to call the helper instead of carrying an inline provider loop.
- Updated `deploy.sh`, `azure.yaml`, and `scripts/dev/preflight-check.sh` to pass the target subscription explicitly when registering providers.
- Hardened `scripts/dev/preflight-check.sh` tool version collection so `set -euo pipefail` cannot stop the check before provider registration.
- Changed `infra/main.parameters.json` so `API_CLIENT_ID` defaults to an empty bootstrap value instead of blocking `azd up` before postprovision.
- Updated `scripts/dev/postprovision.sh` to run `scripts/dev/setup-app-registration.sh` when `API_CLIENT_ID` is missing, then use the persisted value for frontend build args and Container App env vars.
- Updated `scripts/dev/setup-app-registration.sh` to preserve existing SPA redirect URIs and add the deployed Container App URL through `ADDITIONAL_REDIRECT_URIS`.
- Added root `deploy.sh`, which checks Azure CLI login, ensures `azd` is signed in as the same user, creates/selects `AZD_ENV_NAME=elb-dashboard`, sets the default location/subscription/tenant, runs `azd up`, and opens the deployed Container App URL.
- Updated preflight and direct deployment scripts to call the provider helper so non-`azd up` paths do not skip provider registration.
- Updated `README.md` and `docs/get-started.md` to present `azd up` as the deployment command after sign-in.

## Validation evidence

- `bash -n scripts/dev/register-providers.sh scripts/dev/preflight-check.sh scripts/dev/quick-deploy.sh scripts/dev/postprovision.sh scripts/dev/setup-app-registration.sh` -> passed.
- `PROVIDER_REGISTRATION_TIMEOUT_SECONDS=60 bash scripts/dev/register-providers.sh --subscription 577d6332-de48-4a30-be66-dded26a712ea` -> deployment providers reported `ok`, `Microsoft.Compute` and `Microsoft.Quota` reported `ok`, and `Microsoft.ContainerService` registration was started with state `Registering`.
- `bash -n deploy.sh scripts/dev/register-providers.sh scripts/dev/preflight-check.sh scripts/dev/postprovision.sh scripts/dev/quick-deploy.sh && uv run mkdocs build --strict` -> passed; MkDocs still reports the pre-existing informational missing-anchor message for `#phase-2-sign-in-and-create-the-app-registration`.
- `AZURE_LOCATION=eastus PROVIDER_REGISTRATION_TIMEOUT_SECONDS=60 ./deploy.sh --prepare-only` -> selected `ME-MngEnvMCAP982529-jungha-1`, configured the `elb-dashboard` azd environment, and ran provider registration before exiting prepare-only mode.
- `bash -n scripts/dev/preflight-check.sh && scripts/dev/preflight-check.sh` -> passed; provider registration ran from preflight and `Microsoft.ContainerService` reached `Registered`.
- Final provider state check in `577d6332-de48-4a30-be66-dded26a712ea` -> all deployment providers plus `Microsoft.Compute`, `Microsoft.ContainerService`, and `Microsoft.Quota` reported `Registered`.
- `azd env get-values --environment elb-demo` -> confirmed the affected deployment environment is present.
- Provider state check in the demo subscription -> all required namespaces reported `Registered`.
- Redirect URI merge expression used by `scripts/dev/setup-app-registration.sh` -> returned the expected unique URI list in a local `jq` probe.
- `AZURE_LOCATION=eastus ./deploy.sh` in the `az-jungha` Azure CLI context -> provisioned `rg-elb-dashboard`, reused App Registration `elastic-blast-control-plane`, built and pushed `elb-api`, `elb-frontend`, and `elb-terminal`, swapped the six-sidecar Container App layout, and completed with `/api/health` returning 200.
- `curl -fsS --max-time 30 https://ca-elb-control.jollymushroom-0452ee01.eastus.azurecontainerapps.io/api/health` -> returned `{"status":"ok","version":"0.0.1","revision":"ca-elb-control--0000001"}`.
- `curl -fsS --max-time 30 https://ca-elb-control.jollymushroom-0452ee01.eastus.azurecontainerapps.io/runtime-config.js` -> returned runtime config with tenant `184be312-98bf-4c54-903a-e77288f0f984` and a non-empty `VITE_AZURE_CLIENT_ID`.
- Integrated browser opened `https://ca-elb-control.jollymushroom-0452ee01.eastus.azurecontainerapps.io/` and rendered the `ElasticBLAST on Azure` sign-in screen.