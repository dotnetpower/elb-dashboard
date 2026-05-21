# Get Started

This guide takes a fresh clone of `dotnetpower/elb-dashboard` from a clean machine to a deployed ElasticBLAST control plane. It also includes an optional smallest end-to-end BLAST smoke test for tenants where AKS policy allows the workload cluster to stay running.

The production target is one Azure Container App with six sidecars: `frontend`, `api`, `worker`, `beat`, `redis`, and `terminal`. The browser is the primary user interface after deployment. Local commands are only for installing tools, deploying the control plane, and validating the first environment.

## What This Guide Proves

Follow the phases in order for deployment:

1. Install prerequisites on Windows/WSL2, macOS, Linux, or a clean Azure VM.
2. Clone the repository and verify Python/Node dependencies.
3. Create or reuse the Microsoft Entra App Registration.
4. Deploy the bundled Container App with `azd up`.
5. Sign in to the deployed web app.

The remaining phases are an optional smoke test after deployment:

6. Build the ElasticBLAST runtime images in ACR.
7. Prepare the small `16S_ribosomal_RNA` BLAST database.
8. Provision the smallest practical AKS cluster.
9. Submit a small `blastn` job and download the result.
10. Clean up the smoke resources, or lock the platform down for steady state.

If your tenant policy stops or blocks AKS clusters, stop after Phase 5. Deployment is validated by `/api/health`, the six-sidecar Container App layout, and successful sign-in to the dashboard.

## Cost And Cleanup Guardrails

The default control-plane deployment is roughly USD 130/month in `koreacentral` before BLAST workload usage. The smoke test adds an AKS cluster with:

- system pool: `Standard_D2s_v3`, 1 node
- BLAST workload pool: `Standard_D8s_v3`, 1 node
- database: `16S_ribosomal_RNA`, about 18 MB in Storage

Delete or stop the AKS cluster after the smoke run if you are not actively using it. Use `azd down --purge --force` only when you want to remove the whole control plane.

## Recommended Host

- Windows: use WSL2 with Ubuntu 24.04 or 22.04. Run project commands inside WSL.
- macOS or Linux: use your normal terminal.
- Azure validation VM: use Ubuntu 24.04 with a VM size such as `Standard_D4s_v5`. This is useful when you want to prove the guide from a clean OS image, including the full backend test suite and web build. See [Appendix A](#appendix-a-clean-azure-vm-validation).
- Azure deployment: Docker is not required locally. Image builds run in Azure Container Registry with `az acr build`.
- Local full-stack debugging: Docker is optional but recommended, because local Redis and Docker Compose use it.

## Prerequisites

| Requirement | Version | Needed for | Notes |
| --- | --- | --- | --- |
| Git | 2.x | clone | Use the WSL package on Windows. |
| Bash | 5.x | helper scripts | Native on Linux/macOS. Use WSL on Windows. |
| Azure CLI | 2.81+ | Azure login and deployment hooks | Command: `az`. |
| Azure Developer CLI | 1.10+ | `azd up` deployment | Command: `azd`. |
| uv | 0.9+ | Python environment and tests | Do not use `pip install` for this repo. |
| Python | 3.12.x | backend | Installed and pinned by `uv`. |
| Node.js | 20 LTS | web app | Use npm; the repo includes `web/package-lock.json`. |
| jq | any recent version | setup scripts | Used by App Registration and validation scripts. |
| curl | any recent version | installers and smoke checks | Usually already present on Linux/macOS. |
| Docker | 20.x+ | optional local Redis / Compose | Not required for `azd up`. |
| VS Code | current | optional | Useful because this repo includes local dev tasks. |

You also need an Azure subscription where you can create resource groups, managed identities, role assignments, ACR, Storage, and Container Apps. First-time deployment is easiest with `Owner`, or with `Contributor` plus `User Access Administrator`. The optional BLAST smoke test also needs permission and tenant policy allowance to create and run AKS.

If your tenant blocks App Registration creation or admin consent, ask an Entra administrator to run the App Registration step or grant consent for you.

## Phase 0: Install Tools

<details open markdown="1">
<summary>Windows With WSL2</summary>

Run these commands from PowerShell as an administrator:

```powershell
wsl --install -d Ubuntu-24.04
winget install --id Docker.DockerDesktop -e
```

Restart if Windows asks you to. Open Ubuntu from the Start menu, then run all remaining project commands inside Ubuntu.

Inside Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y git curl jq unzip ca-certificates gnupg lsb-release

curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
curl -fsSL https://aka.ms/install-azd.sh | bash
curl -LsSf https://astral.sh/uv/install.sh | sh

curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

exec $SHELL -l
```

If you installed Docker Desktop, open Docker Desktop settings and enable WSL integration for your Ubuntu distribution. Docker is only needed for local Redis, worker/beat, and Compose workflows.

Recommended Git setting inside WSL:

```bash
git config --global core.autocrlf input
```

Clone inside the WSL Linux filesystem, for example under `~/dev`, not under `/mnt/c/...`. This avoids slow file watching and line-ending surprises.

</details>

<details markdown="1">
<summary>macOS</summary>

Using Homebrew:

```bash
brew update
brew install git jq curl node@20 uv azure-cli
brew tap azure/azd
brew install azd

# If node is not on PATH after installation:
echo 'export PATH="$(brew --prefix node@20)/bin:$PATH"' >> ~/.zshrc
exec $SHELL -l
```

Docker Desktop is optional. Install it only if you want local Redis or Docker Compose workflows.

</details>

<details markdown="1">
<summary>Ubuntu Or Debian</summary>

```bash
sudo apt-get update
sudo apt-get install -y git curl jq unzip ca-certificates gnupg lsb-release

curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
curl -fsSL https://aka.ms/install-azd.sh | bash
curl -LsSf https://astral.sh/uv/install.sh | sh

curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

exec $SHELL -l
```

Install Docker only if you want local Redis or Docker Compose workflows.

</details>

## Phase 1: Clone And Verify The Repository

Clone the repository:

```bash
mkdir -p ~/dev
cd ~/dev
git clone https://github.com/dotnetpower/elb-dashboard.git
cd elb-dashboard
```

Verify the tools:

```bash
az --version | head -1
azd version
uv --version
node --version
npm --version
jq --version
git --version
```

Expected highlights:

- `az` is `2.81.0` or newer.
- `azd` is `1.10.0` or newer.
- `uv` is `0.9.0` or newer.
- `node --version` starts with `v20.` for the pinned path.

Install and verify the pinned Python runtime:

```bash
uv python install 3.12
uv sync --all-groups
uv run python --version
```

`uv run python --version` should print Python `3.12.x`.

Install the web dependencies:

```bash
cd web
npm ci
cd ..
```

Run the backend tests when you are preparing a development machine:

```bash
uv run pytest -q api/tests
```

For a quick backend-only check:

```bash
scripts/dev/local-run.sh api
```

In another terminal:

```bash
curl http://127.0.0.1:8085/api/health
```

Expected result: HTTP `200` with a JSON body containing `"status":"ok"`. The helper writes logs under `.logs/local/latest/`. Start with `.logs/local/latest/api.log` when something fails.

## Phase 2: Sign In

Sign in and select a subscription:

```bash
az login
az account set --subscription "<your-subscription-name-or-id>"
```

The deployment creates or reuses the App Registration automatically during `azd up`. You only need to sign in with an account that can create App Registrations in the tenant, or ask an Entra administrator to create the app once and provide `API_CLIENT_ID`.

## Phase 3: Deploy The Control Plane With azd

Use an interactive Azure Developer CLI login for deployment:

```bash
azd auth login --use-device-code --tenant-id "$(az account show --query tenantId -o tsv)"
```

Do not rely on `azd auth login --managed-identity` for the clean-machine deployment path. It can acquire an ARM token, but `azd` may still fail while resolving the deployer principal for Bicep role-assignment parameters. Interactive `azd` login is the supported path for this guide.

Create the standard Azure Developer CLI environment. The resource names use the `elb-dashboard` / `elbdashboard` prefix:

```bash
azd env new elb-dashboard
azd env set AZURE_LOCATION koreacentral
azd env set ALLOWED_ORIGINS ""
azd env set LOCKDOWN_PRIVATE_NETWORKING false
```

Leave `DEPLOYER_PRINCIPAL_ID` unset unless your administrator explicitly gives you a principal object id to use. The default parameters treat it as optional, which avoids forcing principal lookup in managed-identity-only validation environments.

Optionally run the preflight check. It validates your tools and Azure context, then idempotently registers the Azure resource providers required by the deployment. It also starts first-run workflow provider registration for VM quota checks and AKS provisioning (`Microsoft.Compute`, `Microsoft.ContainerService`, and `Microsoft.Quota`):

```bash
scripts/dev/preflight-check.sh
```

For the shortest fresh-clone path, run the bootstrap wrapper. It checks `az account show`, starts `az login` if needed, prepares the `elb-dashboard` azd environment so the resource group is `rg-elb-dashboard`, runs `azd up`, and opens the deployed Container App URL:

```bash
./deploy.sh
```

If you prefer the raw azd command after preparing/selecting the environment, run:

```bash
azd up
```

What `azd up` does:

The command prints an `azd up progress map` before long-running work starts, then marks the active step as `[n/7]` while it runs.

1. Registers deployment Azure resource providers and starts first-run workflow provider registration.
2. Provisions the bootstrap platform resources from `infra/main.bicep`.
3. Creates or reuses the App Registration if `API_CLIENT_ID` is not set.
4. Validates the platform Storage account and dashboard discovery tags.
5. Builds the API, frontend, and terminal images with `az acr build`.
6. Swaps the Container App to the six-sidecar layout.
7. Waits for `/api/health` and prints the Container App URL.

Check the health endpoint:

```bash
APP_URL=$(azd env get-values | awk -F= '/^CONTAINER_APP_URL=/{gsub(/"/,"",$2); print $2}')
curl -fsS "$APP_URL/api/health" | python -m json.tool
```

Expected result: HTTP `200` with `"status":"ok"`.

If you want to capture the deployment result for later smoke-test commands:

```bash
azd env get-values | tee /tmp/elb-azd-values.env
```

Also confirm the Container App is running the full sidecar layout:

```bash
AZURE_RESOURCE_GROUP=$(azd env get-values | awk -F= '/^AZURE_RESOURCE_GROUP=/{gsub(/"/,"",$2); print $2}')
az containerapp show \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name ca-elb-dashboard \
  --query 'properties.template.containers[].name' \
  -o table
```

Expected containers: `frontend`, `api`, `worker`, `beat`, `redis`, and `terminal`.
If a later `azd provision` changes the app back to a bootstrap-only revision,
restore the sidecar layout before continuing with the smoke test.

At this point the deployment portion is complete. In tenants where AKS is blocked or automatically stopped by policy, pause here and do not continue into the smoke-test phases.

## Phase 4: Add The Deployed Redirect URI

The setup script creates the local redirect URI `http://localhost:8090`. After `azd up` prints the real Container App URL, add that origin as an additional SPA redirect URI.

CLI path:

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

Portal path:

1. Microsoft Entra ID.
2. App registrations.
3. Open the app created by `scripts/dev/setup-app-registration.sh`.
4. Authentication.
5. Single-page application.
6. Add the deployed Container App origin, for example `https://ca-elb-dashboard.<subdomain>.<region>.azurecontainerapps.io`.
7. Save.

Keep `http://localhost:8090` if you also use the local web app.

## Phase 5: Open The Web App

Open the deployed URL printed by `azd up`:

```text
https://ca-elb-dashboard.<subdomain>.<region>.azurecontainerapps.io
```

Sign in with the same tenant that owns the App Registration. The dashboard should load real Azure data from the deployed API sidecar.

If the app signs in locally but not in Azure, re-check the deployed redirect URI from Phase 4.

## Phase 6: Smallest End-To-End BLAST Smoke Test

Use these values for the first run:

| Setting | Value |
| --- | --- |
| Resource group | the `AZURE_RESOURCE_GROUP` from `azd env get-values` |
| Storage account | the `STORAGE_ACCOUNT_NAME` from `azd env get-values` |
| ACR | the `ACR_NAME` from `azd env get-values` |
| AKS cluster name | `elb-smoke-aks` |
| AKS system pool | `Standard_D2s_v3`, 1 node |
| AKS BLAST workload pool | `Standard_D8s_v3`, 1 node |
| BLAST database | `16S_ribosomal_RNA` |
| Program | `blastn` |
| Output format | `5` (XML) |
| Sharding mode | Off |
| Warmup | Off for the first smoke run |

### 6.1 Build The ElasticBLAST Runtime Images

The `azd up` hook builds the control-plane images. ElasticBLAST runtime images are separate and must exist in your ACR before submit.
The `ncbi/elasticblast-job-submit:4.1.0` build is patched during ACR build so
AKS Blob CSI / Blob NFS runs skip the GCP-style VolumeSnapshot step.

In the dashboard:

1. Open the ACR card.
2. Select the ACR created by `azd up`.
3. Click **Build All Images**.
4. Wait until the required tags are present:
   - `ncbi/elb:1.4.0`
   - `ncbi/elasticblast-job-submit:4.1.0`
   - `ncbi/elasticblast-query-split:0.1.4`
   - `elb-openapi:4.9`

CLI spot-check:

```bash
ACR_NAME=$(azd env get-values | awk -F= '/^ACR_NAME=/{gsub(/"/,"",$2); print $2}')
az acr repository show-tags -n "$ACR_NAME" --repository ncbi/elb -o table
az acr repository show-tags -n "$ACR_NAME" --repository ncbi/elasticblast-job-submit -o table
az acr repository show-tags -n "$ACR_NAME" --repository ncbi/elasticblast-query-split -o table
az acr repository show-tags -n "$ACR_NAME" --repository elb-openapi -o table
```

### 6.2 Prepare The Small BLAST Database

In the dashboard:

1. Open the Storage card.
2. Select the Storage account created by `azd up`.
3. Click **Download from NCBI**.
4. Choose **16S ribosomal RNA** (`16S_ribosomal_RNA`).
5. Wait until the database row shows files in `blast-db/16S_ribosomal_RNA/`.

The API starts an asynchronous server-side copy from NCBI's public S3 bucket to your Storage account. The bytes do not pass through the browser and no SAS token is issued to the browser.

Optional CLI spot-check, only while the Storage account is reachable from your
workstation. Skip this check when `publicNetworkAccess` is already `Disabled` or
when network rules block your public IP; the dashboard/API path is the
steady-state verification path.

```bash
STORAGE_ACCOUNT_NAME=$(azd env get-values | awk -F= '/^STORAGE_ACCOUNT_NAME=/{gsub(/"/,"",$2); print $2}')
az storage blob list \
  --auth-mode login \
  --account-name "$STORAGE_ACCOUNT_NAME" \
  --container-name blast-db \
  --prefix 16S_ribosomal_RNA/ \
  --query 'length(@)' \
  -o tsv
```

Expected result when workstation access is allowed: a positive integer.

### 6.3 Create The Small AKS Cluster

In the dashboard:

1. Open the AKS card.
2. Click **Add Cluster**.
3. Use the values from the smoke table:
   - cluster name: `elb-smoke-aks`
   - resource group: the `AZURE_RESOURCE_GROUP` from `azd env get-values`
   - region: same as `AZURE_LOCATION`
   - system VM size: `Standard_D2s_v3`
   - system node count: `1`
   - workload VM size: `Standard_D8s_v3`
   - workload node count: `1`
   - ACR and Storage: the resources created by `azd up`
4. Start provisioning and wait for `provisioningState=Succeeded` and `powerState=Running`.

CLI spot-check:

```bash
AZURE_RESOURCE_GROUP=$(azd env get-values | awk -F= '/^AZURE_RESOURCE_GROUP=/{gsub(/"/,"",$2); print $2}')
az aks show \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name elb-smoke-aks \
  --query '{state:provisioningState,power:powerState.code,nodePools:agentPoolProfiles[].{name:name,vmSize:vmSize,count:count,mode:mode}}' \
  -o jsonc
```

Expected result: `state` is `Succeeded`, `power` is `Running`, and there are two pools: `systempool` and `blastpool`.

The AKS cluster must also have the Blob CSI driver enabled. ElasticBLAST's
Azure PV mode uses the `azureblob-nfs-premium` StorageClass; without it the
`blast-dbs-pvc-rwm` PVC remains pending and the BLAST job never starts.

```bash
az aks show \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name elb-smoke-aks \
  --query 'storageProfile.blobCsiDriver.enabled' \
  -o tsv

az aks get-credentials \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name elb-smoke-aks \
  --overwrite-existing

kubectl get storageclass azureblob-nfs-premium
```

Expected result: the first command prints `true`, and the StorageClass exists.

### 6.4 Sign In Inside The Browser Terminal

Open the Terminal page in the web app. In the browser terminal, run:

```bash
az login --use-device-code
az account set --subscription "<your-subscription-name-or-id>"
az account show --query '{name:name,id:id,user:user.name}' -o table
```

This login is stored in the terminal sidecar's persisted home directory. It is used by the ElasticBLAST CLI path. The API sidecar still uses the shared managed identity for Azure SDK calls.

The terminal image should already have the ElasticBLAST CLI on `PATH`:

```bash
command -v elastic-blast
elastic-blast --version
```

### 6.5 Submit The Tiny BLAST Job

Open **BLAST Submit** and fill the form:

1. Program: `blastn`.
2. Cluster: `elb-smoke-aks`.
3. Database: `16S_ribosomal_RNA` or `blast-db/16S_ribosomal_RNA/16S_ribosomal_RNA`.
4. Query: paste this FASTA:

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

5. Output format: `5`.
6. E-value: keep the default `0.05`.
7. Max target sequences: keep the default `100`.
8. Sharding mode: Off.
9. Warmup: Off.
10. Run the pre-flight check.
11. Click **Run BLAST**.

The job detail page should show the phases moving through queued, config, submit, running, and completed states. For this tiny smoke run, completion time depends mostly on AKS image pulls and first-run cluster setup rather than query size.

### 6.6 Download Or Inspect The Result

When the job reaches `Completed`:

1. Open the job detail page.
2. Open the Results section.
3. Download or inspect the result file.
4. Confirm the XML contains `<BlastOutput>` and at least one hit or an explicit no-hit result for the query.

The browser downloads through the API sidecar. The browser should not receive a Storage SAS URL.

## Phase 7: Lock Down Networking After The First Smoke Test

The steady-state posture is private networking: Storage, Key Vault, and ACR are
reached by the Container App over private endpoints. If your first deployment
temporarily allowed public bootstrap access, flip the steady-state private
networking switch after the control plane and smoke test succeed:

```bash
azd env set LOCKDOWN_PRIVATE_NETWORKING true
azd provision
```

After this provision, the Container App reaches Storage, Key Vault, and ACR over
private endpoints. Do not add a dashboard button or production code path that
enables public Storage access.

After lockdown, local `az storage blob list` commands from your laptop may fail with network access errors. Use the dashboard/API path from inside the Container App for steady-state data access.

## Phase 8: Stop Or Delete Smoke Resources

To stop the smoke AKS cluster from the CLI:

```bash
AZURE_RESOURCE_GROUP=$(azd env get-values | awk -F= '/^AZURE_RESOURCE_GROUP=/{gsub(/"/,"",$2); print $2}')
az aks stop --resource-group "$AZURE_RESOURCE_GROUP" --name elb-smoke-aks
```

To delete only the smoke AKS cluster:

```bash
AZURE_RESOURCE_GROUP=$(azd env get-values | awk -F= '/^AZURE_RESOURCE_GROUP=/{gsub(/"/,"",$2); print $2}')
az aks delete --resource-group "$AZURE_RESOURCE_GROUP" --name elb-smoke-aks --yes --no-wait
```

To remove the entire control plane:

```bash
azd down --purge --force
```

## Day-To-Day Local Commands

Backend tests:

```bash
uv run pytest -q api/tests
```

Backend lint:

```bash
uv run ruff check api
```

Frontend build:

```bash
cd web
npm run build
cd ..
```

Start local API:

```bash
scripts/dev/local-run.sh api
```

Start local web:

```bash
scripts/dev/local-run.sh web
```

Run API smoke test against the local API:

```bash
scripts/dev/local-run.sh smoke
```

Start optional local Redis, worker, and beat:

```bash
scripts/dev/local-run.sh redis
scripts/dev/local-run.sh worker
scripts/dev/local-run.sh beat
```

Stop local Redis:

```bash
docker rm -f elb-dev-redis
```

## Appendix A: Clean Azure VM Validation

Use this appendix when you want to prove the guide from a fresh Ubuntu VM. This is a maintainer/operator validation path; it is not part of the deployed control-plane architecture.

Set variables on your local machine:

```bash
export VALIDATION_RG=rg-elb-docs-verify
export VALIDATION_LOCATION=koreacentral
export VALIDATION_VM=vm-elb-docs-verify
export VALIDATION_ADMIN=azureuser
export CALLER_IP=$(curl -fsS https://api.ipify.org)
```

Create the VM with SSH restricted to your current public IP:

```bash
az group create \
  --name "$VALIDATION_RG" \
  --location "$VALIDATION_LOCATION" \
  --tags app=elb-dashboard role=docs-validation managedBy=manual

az vm create \
  --resource-group "$VALIDATION_RG" \
  --name "$VALIDATION_VM" \
  --image Ubuntu2404 \
  --size Standard_D4s_v5 \
  --admin-username "$VALIDATION_ADMIN" \
  --generate-ssh-keys \
  --public-ip-sku Standard \
  --nsg-rule NONE \
  --tags app=elb-dashboard role=docs-validation managedBy=manual

NIC_ID=$(az vm show \
  --resource-group "$VALIDATION_RG" \
  --name "$VALIDATION_VM" \
  --query 'networkProfile.networkInterfaces[0].id' \
  -o tsv)
NSG_ID=$(az network nic show --ids "$NIC_ID" --query 'networkSecurityGroup.id' -o tsv)
NSG_RG=$(echo "$NSG_ID" | awk -F/ '{print $5}')
NSG_NAME=$(basename "$NSG_ID")

az network nsg rule create \
  --resource-group "$NSG_RG" \
  --nsg-name "$NSG_NAME" \
  --name AllowSshFromCaller \
  --priority 100 \
  --access Allow \
  --protocol Tcp \
  --direction Inbound \
  --source-address-prefixes "${CALLER_IP}/32" \
  --source-port-ranges '*' \
  --destination-address-prefixes '*' \
  --destination-port-ranges 22

VALIDATION_IP=$(az vm show \
  --resource-group "$VALIDATION_RG" \
  --name "$VALIDATION_VM" \
  --show-details \
  --query publicIps \
  -o tsv)

ssh -o StrictHostKeyChecking=accept-new "$VALIDATION_ADMIN@$VALIDATION_IP"
```

Inside the VM, run the Linux setup and clone phases exactly as documented:

```bash
sudo apt-get update
sudo apt-get install -y git curl jq unzip ca-certificates gnupg lsb-release

curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
curl -fsSL https://aka.ms/install-azd.sh | bash
curl -LsSf https://astral.sh/uv/install.sh | sh

curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

export PATH="$HOME/.local/bin:$PATH"

az --version | head -1
azd version
uv --version
node --version
npm --version
jq --version
git --version

mkdir -p ~/dev
cd ~/dev
git clone https://github.com/dotnetpower/elb-dashboard.git
cd elb-dashboard

uv python install 3.12
uv sync --all-groups
uv run python --version

cd web
npm ci
cd ..
```

To validate the Azure deployment phases from the VM, sign in interactively from the VM and continue at [Phase 2](#phase-2-sign-in-and-create-the-app-registration):

```bash
az login --use-device-code
az account set --subscription "<your-subscription-name-or-id>"
azd auth login --use-device-code --tenant-id "$(az account show --query tenantId -o tsv)"
```

Run `azd up` from the VM only after the app registration step has produced `API_CLIENT_ID`. The expected deployment checkpoint is:

- `azd up` finishes successfully.
- `curl "$APP_URL/api/health"` returns HTTP `200` with `"status":"ok"`.
- `az containerapp show ... --query 'properties.template.containers[].name'` lists `frontend`, `api`, `worker`, `beat`, `redis`, and `terminal`.

Do not continue into AKS smoke testing from this appendix when tenant policy stops or blocks AKS. The clean VM deployment proof ends at the Container App health and sidecar checks.

When validation is finished, delete the VM resource group:

```bash
az group delete --name "$VALIDATION_RG" --yes --no-wait
```

## Troubleshooting

If a script says `Permission denied`, make sure the executable bit survived the clone:

```bash
chmod +x scripts/dev/*.sh
```

If a script prints `$'\r': command not found`, the checkout has Windows CRLF line endings. In WSL, set:

```bash
git config --global core.autocrlf input
```

Then reclone the repository.

If `uv run python --version` is not Python `3.12.x`, run:

```bash
uv python install 3.12
uv sync --all-groups
```

If `npm ci` fails after changing Node versions, remove only web dependencies and reinstall:

```bash
cd web
rm -rf node_modules
npm ci
cd ..
```

If `scripts/dev/local-run.sh redis` fails on Windows, start Docker Desktop and verify WSL integration is enabled for your Ubuntu distribution.

If `azd up` fails on a role assignment, confirm your account has `Owner` or `User Access Administrator` on the subscription. In restricted tenants, ask an Azure administrator to perform the role assignment step described in `docs/auth.md`. If the failure happens while resolving a deployer principal under managed identity, sign in with `azd auth login --use-device-code` and leave `DEPLOYER_PRINCIPAL_ID` unset unless your administrator provides it.

If the deployed app signs in locally but not in Azure, confirm the deployed Container App origin was added as a SPA redirect URI in the App Registration.

If AKS provisioning succeeds but `kubectl get storageclass azureblob-nfs-premium`
returns `NotFound`, enable the Blob CSI driver or reprovision the cluster with
the dashboard's current AKS task. The smoke run requires Blob NFS for the shared
database/query PVC.

If `submit-jobs` fails with missing `/templates/volume-snapshot*.yaml` or a
VolumeSnapshot readiness error, rebuild `ncbi/elasticblast-job-submit:4.1.0` from
the dashboard ACR card. The build step must copy all templates and patch the
Azure job-submit script to skip snapshots unless `ELB_CLOUD_PROVIDER=gcp`.

If a manual terminal-side submit cannot write `elastic-blast.log`, pass an
explicit writable path:

```bash
elastic-blast submit --cfg /tmp/elastic-blast.ini --logfile /tmp/elastic-blast.log
```

If the local dashboard shows `access_denied` or `network_blocked` against a deployed environment, grant your local `az login` user the local debugging roles:

```bash
scripts/dev/grant-local-rbac.sh
```

Then wait 1-5 minutes for RBAC propagation.

## Validation Log

Use this checklist when updating the deployment path in this guide:

| Step | Evidence |
| --- | --- |
| Tool versions | `az --version`, `azd version`, `uv --version`, `node --version`, `npm --version`, `jq --version` |
| Python setup | `uv python install 3.12`, `uv sync --all-groups`, `uv run python --version` |
| Web setup | `cd web && npm ci` |
| Local backend | `scripts/dev/local-run.sh api`, `curl http://127.0.0.1:8085/api/health` |
| Azure deployment | `azd up`, `curl "$APP_URL/api/health"` |

Optional smoke-test evidence, only in tenants where AKS is allowed to run:

| Step | Evidence |
| --- | --- |
| Runtime images | ACR card shows all required tags or `az acr repository show-tags` confirms them |
| Database | Storage card shows `16S_ribosomal_RNA` or blob list count is positive |
| AKS | `az aks show` returns `Succeeded` and `Running` |
| AKS Blob CSI | `az aks show --query storageProfile.blobCsiDriver.enabled` prints `true`; `kubectl get storageclass azureblob-nfs-premium` succeeds |
| Terminal | browser terminal runs `az account show` |
| BLAST result | job reaches `Completed`; downloaded result contains `<BlastOutput>` |

Last deployment-only maintainer validation: 2026-05-17 in `koreacentral`, using
a clean Ubuntu 24.04 VM sized `Standard_D4s_v5`. The clean VM completed
repository clone/setup, `uv run pytest -q api/tests` with 583 passing tests,
`cd web && npm test -- --run` with 152 passing tests, `npm run build`,
`uv run ruff check api`, and `azd up`. The deployed Container App URL was
`https://ca-elb-dashboard.purplestone-ed1e00cc.koreacentral.azurecontainerapps.io`,
`/api/health` returned `{"status":"ok","version":"0.0.1"}`, and the app
revision contained the expected six sidecars: `api`, `frontend`, `worker`,
`beat`, `redis`, and `terminal`.

AKS and BLAST submit validation were intentionally paused after deployment in a
tenant where policy stops AKS. Resume the optional smoke phases only when AKS is
allowed to remain running long enough for the job lifecycle.

## Next Reading

- [Architecture reference](./container-apps-migration.md)
- [Authentication and RBAC](./auth.md)
- [Local development helpers](../scripts/dev/README.md)
- [Agent navigation map](../AGENTS.md)
