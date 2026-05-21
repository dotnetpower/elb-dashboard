# Deployment Reference

This reference is for platform maintainers, administrators, and developers who need the details behind the quick-start deployment path.

Researchers who only need to deploy and open the dashboard should start with [Get Started](get-started.md).

## Deployment Shape

The production target is one Azure Container App with six sidecars: `frontend`, `api`, `worker`, `beat`, `redis`, and `terminal`. The browser is the primary user interface after deployment. Local commands are for installing tools, deploying the control plane, and validating the first environment.

Image builds run in Azure Container Registry with `az acr build`; Docker is not required for the normal deployment path.

## Tool Installation

Minimum tools for deployment:

| Requirement | Needed for | Notes |
| --- | --- | --- |
| Git | clone | Use the WSL package on Windows. |
| Bash | helper scripts | Native on Linux/macOS; use WSL on Windows. |
| Azure CLI | Azure login and deployment hooks | Command: `az`; use 2.81 or newer. |
| Azure Developer CLI | `azd up` deployment | Command: `azd`; use 1.10 or newer. |
| jq | setup scripts | Used by App Registration and validation scripts. |
| curl | installers and smoke checks | Usually already present on Linux/macOS. |

Development and validation also use `uv`, Python 3.12, Node.js 20, npm, and optional Docker.

### Windows With WSL2

Run from PowerShell as an administrator:

```powershell
wsl --install -d Ubuntu-24.04
```

Then run project commands inside Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y git curl jq unzip ca-certificates gnupg lsb-release

curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
curl -fsSL https://aka.ms/install-azd.sh | bash

exec $SHELL -l
```

Clone inside the WSL Linux filesystem, for example under `~/dev`, not under `/mnt/c/...`.

### macOS

Using Homebrew:

```bash
brew update
brew install git jq curl azure-cli
brew tap azure/azd
brew install azd
```

### Ubuntu Or Debian

```bash
sudo apt-get update
sudo apt-get install -y git curl jq unzip ca-certificates gnupg lsb-release

curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
curl -fsSL https://aka.ms/install-azd.sh | bash

exec $SHELL -l
```

## Clone And Check Tools

```bash
mkdir -p ~/dev
cd ~/dev
git clone https://github.com/dotnetpower/elb-dashboard.git
cd elb-dashboard

az --version | head -1
azd version
jq --version
git --version
```

For development machines, also install and verify the pinned runtime stacks:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12
uv sync --all-groups

cd web
npm ci
cd ..
```

## One-Command Deployment

The recommended deployment entry point is still:

```bash
./deploy.sh
```

Useful environment overrides:

```bash
export AZD_ENV_NAME=elb-dashboard
export AZURE_LOCATION=koreacentral
export LOCKDOWN_PRIVATE_NETWORKING=false
export ALLOWED_ORIGINS=""
export ELB_EXISTING_RG_ACTION=number  # delete, number, or abort
./deploy.sh
```

Prepare only, without running `azd up`:

```bash
./deploy.sh --prepare-only
```

## Manual azd Path

Use this path only when you need to control each value yourself.

```bash
az login
az account set --subscription "<your-subscription-name-or-id>"

azd auth login --use-device-code --tenant-id "$(az account show --query tenantId -o tsv)"

azd env new elb-dashboard
azd env set AZURE_LOCATION koreacentral
azd env set ALLOWED_ORIGINS ""
azd env set LOCKDOWN_PRIVATE_NETWORKING false

scripts/dev/preflight-check.sh
azd up
```

Leave `DEPLOYER_PRINCIPAL_ID` unset unless your administrator explicitly gives you a principal object id to use.

## What azd up Does

The command prints an `azd up progress map` before long-running work starts, then marks the active step as `[n/8]` while it runs.

1. Registers deployment Azure resource providers and starts first-run workflow provider registration.
2. Checks `rg-elb-dashboard`; if it already contains resources, asks whether to delete it, deploy to a numbered group such as `rg-elb-dashboard-01`, or abort.
3. Provisions the bootstrap platform resources from `infra/main.bicep`.
4. Creates or reuses the App Registration if `API_CLIENT_ID` is not set.
5. Validates the platform Storage account and dashboard discovery tags.
6. Builds the API, frontend, and terminal images with `az acr build`.
7. Swaps the Container App to the six-sidecar layout.
8. Waits for `/api/health` and prints the Container App URL.

Check the health endpoint:

```bash
APP_URL=$(azd env get-values | awk -F= '/^CONTAINER_APP_URL=/{gsub(/"/,"",$2); print $2}')
curl -fsS "$APP_URL/api/health" | python -m json.tool
```

Confirm the sidecar layout:

```bash
AZURE_RESOURCE_GROUP=$(azd env get-values | awk -F= '/^AZURE_RESOURCE_GROUP=/{gsub(/"/,"",$2); print $2}')
az containerapp show \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name ca-elb-dashboard \
  --query 'properties.template.containers[].name' \
  -o table
```

Expected containers: `frontend`, `api`, `worker`, `beat`, `redis`, and `terminal`.

## Redirect URI After Deployment

The setup script creates the local redirect URI `http://localhost:8090`. After `azd up` prints the real Container App URL, add that origin as an additional SPA redirect URI if the helper did not already persist it.

```bash
API_CLIENT_ID=$(azd env get-values | awk -F= '/^API_CLIENT_ID=/{gsub(/"/,"",$2); print $2}')
APP_URL=$(azd env get-values | awk -F= '/^CONTAINER_APP_URL=/{gsub(/"/,"",$2); print $2}')
APP_OBJECT_ID=$(az ad app show --id "$API_CLIENT_ID" --query id -o tsv)

REDIRECTS=$(az rest \
  --method GET \
  --uri "https://graph.microsoft.com/v1.0/applications/$APP_OBJECT_ID?\$select=spa" \
  | jq -c --arg uri "$APP_URL" '(.spa.redirectUris // []) + [$uri] | unique')

az rest \
  --method PATCH \
  --uri "https://graph.microsoft.com/v1.0/applications/$APP_OBJECT_ID" \
  --headers "Content-Type=application/json" \
  --body "{\"spa\":{\"redirectUris\":$REDIRECTS}}"
```

Keep `http://localhost:8090` if you also use the local web app.

## Optional BLAST Smoke Test

Run this only in tenants where AKS is allowed to remain running long enough for the job lifecycle.

First-run values:

| Setting | Value |
| --- | --- |
| AKS cluster name | `elb-smoke-aks` |
| AKS system pool | `Standard_D2s_v3`, 1 node |
| AKS BLAST workload pool | `Standard_D8s_v3`, 1 node |
| BLAST database | `16S_ribosomal_RNA` |
| Program | `blastn` |
| Output format | `5` (XML) |
| Sharding mode | Off |
| Warmup | Off for the first smoke run |

Browser path:

1. Build the ElasticBLAST runtime images from the ACR card.
2. Download `16S_ribosomal_RNA` from the Storage card.
3. Create `elb-smoke-aks` from the AKS card.
4. Sign in inside the browser terminal with `az login --use-device-code`.
5. Submit a tiny `blastn` job from New Search.
6. Open the result and confirm the XML contains `<BlastOutput>`.

Required runtime image tags:

- `ncbi/elb:1.4.0`
- `ncbi/elasticblast-job-submit:4.1.0`
- `ncbi/elasticblast-query-split:0.1.4`
- `elb-openapi:4.9`

Tiny query for the first smoke run:

```fasta
>example_16S_rRNA Escherichia coli 16S ribosomal RNA partial sequence
AGAGTTTGATCCTGGCTCAGATTGAACGCTGGCGGCAGGCCTAACACATGCAAGTCGAAC
GGTAACAGGAAGAAGCTTGCTTCTTTGCTGACGAGTGGCGGACGGGTGAGTAATGTCTG
GGAAACTGCCTGATGGAGGGGGATAACTACTGGAAACGGTAGCTAATACCGCATAACGTCG
CAAGACCAAAGAGGGGGACCTTAGGGCCTCTTGCCATCGGATGTGCCCAGATGGGATTAGC
TAGTAGGTGGGGTAACGGCTCACCTAGGCGACGATCCCTAGCTGGTCTGAGAGGATGACC
AGCCACACTGGAACTGAGACACGGTCCAGACTCCTACGGGAGGCAGCAGTGGGGAATATTG
CACAATGGGCGCAAGCCTGATGCAGCCATGCCGCGTGTATGAAGAAGGCCTTCGGGTTGT
AAAGTACTTTCAGCGGGGAGGAAGGGAGTAAAGTTAATACCTTTGCTCATTGA
```

The browser downloads results through the API sidecar. It should not receive a Storage SAS URL.

## Network Lockdown

The steady-state posture is private networking: Storage, Key Vault, and ACR are reached by the Container App over private endpoints.

After the control plane and smoke test succeed:

```bash
azd env set LOCKDOWN_PRIVATE_NETWORKING true
azd provision
```

After lockdown, local `az storage blob list` commands from a laptop may fail with network access errors. Use the dashboard/API path from inside the Container App for steady-state data access.

## Cleanup

Stop the smoke AKS cluster:

```bash
AZURE_RESOURCE_GROUP=$(azd env get-values | awk -F= '/^AZURE_RESOURCE_GROUP=/{gsub(/"/,"",$2); print $2}')
az aks stop --resource-group "$AZURE_RESOURCE_GROUP" --name elb-smoke-aks
```

Delete only the smoke AKS cluster:

```bash
az aks delete --resource-group "$AZURE_RESOURCE_GROUP" --name elb-smoke-aks --yes --no-wait
```

Remove the entire control plane:

```bash
azd down --purge --force
```

## Local Development Commands

```bash
uv run pytest -q api/tests
uv run ruff check api

cd web
npm run build
cd ..

scripts/dev/local-run.sh api
scripts/dev/local-run.sh web
scripts/dev/local-run.sh smoke
```

Optional local Redis, worker, and beat:

```bash
scripts/dev/local-run.sh redis
scripts/dev/local-run.sh worker
scripts/dev/local-run.sh beat
```

## Clean Azure VM Validation

Use a clean Ubuntu 24.04 VM when you need to prove the guide from a fresh OS image. This is a maintainer validation path, not part of the deployed control-plane architecture.

Recommended VM size: `Standard_D4s_v5`.

Validation checkpoints:

- Tool versions print successfully.
- `uv sync --all-groups` creates the Python 3.12 environment.
- `uv run pytest -q api/tests` passes.
- `cd web && npm run build` passes.
- `azd up` finishes successfully.
- `curl "$APP_URL/api/health"` returns HTTP `200` with `"status":"ok"`.
- The Container App revision lists the expected six sidecars.

Delete the validation VM resource group when finished.

## Troubleshooting

If a script says `Permission denied`, make sure the executable bit survived the clone:

```bash
chmod +x scripts/dev/*.sh deploy.sh
```

If a script prints `$'\r': command not found`, the checkout has Windows CRLF line endings. In WSL, set:

```bash
git config --global core.autocrlf input
```

Then reclone the repository.

If `azd up` fails on a role assignment, confirm your account has `Owner` or `User Access Administrator` on the subscription. In restricted tenants, ask an Azure administrator to perform the role assignment step described in [Auth](auth.md).

If the deployed app signs in locally but not in Azure, confirm the deployed Container App origin was added as a SPA redirect URI in the App Registration.

If AKS provisioning succeeds but `kubectl get storageclass azureblob-nfs-premium` returns `NotFound`, enable the Blob CSI driver or reprovision the cluster with the dashboard's current AKS task.

If `submit-jobs` fails with missing `/templates/volume-snapshot*.yaml` or a VolumeSnapshot readiness error, rebuild `ncbi/elasticblast-job-submit:4.1.0` from the dashboard ACR card.

If a manual terminal-side submit cannot write `elastic-blast.log`, pass an explicit writable path:

```bash
elastic-blast submit --cfg /tmp/elastic-blast.ini --logfile /tmp/elastic-blast.log
```

If the local dashboard shows `access_denied` or `network_blocked` against a deployed environment, grant your local `az login` user the local debugging roles:

```bash
scripts/dev/grant-local-rbac.sh
```

Then wait 1-5 minutes for RBAC propagation.