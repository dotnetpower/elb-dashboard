# Get Started

This guide takes a fresh clone of `dotnetpower/elb-dashboard` from zero to a working local development setup or a first Azure deployment.

The project runs a browser-only control plane for ElasticBLAST on Azure. The production target is one Azure Container App with six sidecars: `frontend`, `api`, `worker`, `beat`, `redis`, and `terminal`.

## Recommended Path

- Windows: use WSL2 with Ubuntu 24.04 or 22.04. The repository's helper scripts are Bash scripts, so WSL is the supported Windows path.
- macOS or Linux: use your normal terminal.
- Azure deployment: Docker is not required locally. The deployment builds images in Azure Container Registry with `az acr build`.
- Local full-stack debugging: Docker is optional but recommended, because local Redis and Docker Compose use it.

## What You Need

| Requirement | Version | Needed for | Notes |
| --- | --- | --- | --- |
| Git | 2.x | clone | Use the WSL package on Windows. |
| Bash | 5.x | helper scripts | Native on Linux/macOS. Use WSL on Windows. |
| Azure CLI | 2.81+ | Azure login and deployment hooks | Command: `az`. |
| Azure Developer CLI | 1.10+ | `azd up` deployment | Command: `azd`. |
| uv | 0.9+ | Python environment and tests | Do not use `pip install` for this repo. |
| Python | 3.12.x | backend | `.python-version` pins `3.12`; `pyproject.toml` requires `>=3.12,<3.13`. |
| Node.js | 20 LTS | web app | Use npm; the repo includes `web/package-lock.json`. |
| jq | any recent version | setup scripts | Used by App Registration and validation scripts. |
| curl | any recent version | installers and smoke checks | Usually already present on Linux/macOS. |
| Docker | 20.x+ | optional local Redis / Compose | Not required for `azd up`. |
| VS Code | current | optional | Useful because this repo includes local dev tasks. |

You also need an Azure subscription where you can create resource groups and resources. First-time deployment is easiest with `Owner`, or with `Contributor` plus `User Access Administrator`, because the Bicep template creates managed identities and role assignments.

If your tenant blocks App Registration creation or admin consent, ask an Entra administrator to run the App Registration step or grant consent for you.

## Windows Setup

Run these commands from PowerShell as an administrator first:

```powershell
wsl --install -d Ubuntu-24.04
winget install --id Docker.DockerDesktop -e
```

Restart if Windows asks you to. Open Ubuntu from the Start menu, then run all project commands inside Ubuntu.

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

## macOS Setup

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

## Linux Setup

Ubuntu or Debian:

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

## Verify Your Tools

From the repository root after cloning:

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
- `node --version` starts with `v20.`.
- `uv --version` is `0.9.0` or newer.

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

## Clone The Repository

```bash
mkdir -p ~/dev
cd ~/dev
git clone https://github.com/dotnetpower/elb-dashboard.git
cd elb-dashboard
```

## First Local Backend Check

Run the backend tests:

```bash
uv sync --all-groups
uv run pytest -q api/tests
```

Start the local API:

```bash
scripts/dev/local-run.sh api
```

In another terminal:

```bash
curl http://127.0.0.1:8080/api/health
```

Expected result: HTTP `200` with a JSON body containing `"status":"ok"`.

The helper writes logs under `.logs/local/latest/`. Start with `.logs/local/latest/api.log` when something fails.

## First Local Web Check

Create the Microsoft Entra App Registration first if you want real MSAL login locally:

```bash
az login
scripts/dev/setup-app-registration.sh
```

The script writes `web/.env.local` and prints the App ID. Then start the API and web app in two terminals:

```bash
scripts/dev/local-run.sh api
```

```bash
scripts/dev/local-run.sh web
```

Open:

```text
http://127.0.0.1:8090
```

The Vite dev server proxies `/api/*` to the local API on `127.0.0.1:8080`.

## Optional Local Worker And Beat

Celery worker and beat need Redis. The helper can start Redis in Docker:

```bash
scripts/dev/local-run.sh redis
scripts/dev/local-run.sh worker
scripts/dev/local-run.sh beat
```

You can also use the VS Code task `backend: start (api+worker+beat)`.

For the closest local mirror of the bundled Container App, use Compose:

```bash
scripts/dev/local-run.sh compose-full -- up --build
```

The Compose API binds to `http://127.0.0.1:18080` to avoid colliding with the normal local API on port `8080`.

## Deploy To Azure

Before deploying, understand the default cost. The default sizing is roughly USD 130/month in `koreacentral`; actual cost depends on region, usage, retention, and Azure pricing changes.

Sign in and select a subscription:

```bash
az login
az account set --subscription "<your-subscription-name-or-id>"
```

Create or reuse the App Registration:

```bash
scripts/dev/setup-app-registration.sh
```

Copy the printed App ID. Then create the Azure Developer CLI environment:

```bash
azd env new elb-prod
azd env set AZURE_LOCATION koreacentral
azd env set API_CLIENT_ID <app-id-from-setup-app-registration>
azd env set ALLOWED_ORIGINS ""
azd env set LOCKDOWN_PRIVATE_NETWORKING false
```

Run the preflight check:

```bash
scripts/dev/preflight-check.sh
```

Deploy:

```bash
azd up
```

What `azd up` does:

1. Registers required Azure resource providers.
2. Provisions the platform resources from `infra/main.bicep`.
3. Builds the API, frontend, and terminal images with `az acr build`.
4. Swaps the Container App to the six-sidecar layout.
5. Prints the Container App URL.

Check the health endpoint:

```bash
curl https://<container-app-fqdn>/api/health
```

Expected result: HTTP `200` with `"status":"ok"`.

## Add The Deployed Redirect URI

The setup script creates the local redirect URI `http://localhost:8090`. After `azd up` prints the real Container App URL, add that URL as an additional SPA redirect URI in the App Registration:

```text
https://<container-app-fqdn>
```

Portal path:

1. Microsoft Entra ID.
2. App registrations.
3. Open the app created by `scripts/dev/setup-app-registration.sh`.
4. Authentication.
5. Single-page application.
6. Add the deployed Container App origin as a redirect URI.
7. Save.

Keep `http://localhost:8090` if you also use the local web app.

## Lock Down Networking After The First Deploy

The first deploy keeps Storage, Key Vault, and ACR reachable enough for the operator-side bootstrap to build images and seed configuration. After that succeeds, flip the steady-state private networking switch:

```bash
azd env set LOCKDOWN_PRIVATE_NETWORKING true
azd provision
```

After this second provision, the Container App reaches Storage, Key Vault, and ACR over private endpoints. Do not add a dashboard button or production code path that enables public Storage access.

## Day-To-Day Commands

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

Stop local Redis:

```bash
docker rm -f elb-dev-redis
```

Tear down the Azure deployment:

```bash
azd down --purge --force
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

If `azd up` fails on a role assignment, confirm your account has `Owner` or `User Access Administrator` on the subscription. In restricted tenants, ask an Azure administrator to perform the role assignment step described in `docs/auth.md`.

If the deployed app signs in locally but not in Azure, confirm the deployed Container App origin was added as a SPA redirect URI in the App Registration.

If the local dashboard shows `access_denied` or `network_blocked` against a deployed environment, grant your local `az login` user the local debugging roles:

```bash
scripts/dev/grant-local-rbac.sh
```

Then wait 1-5 minutes for RBAC propagation.

## Next Reading

- [Architecture reference](./container-apps-migration.md)
- [Authentication and RBAC](./auth.md)
- [Local development helpers](../scripts/dev/README.md)
- [Agent navigation map](../AGENTS.md)
