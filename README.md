# elb-dashboard

Browser-only control plane for [ElasticBLAST on Azure](https://github.com/dotnetpower/elastic-blast-azure).

A researcher signs in through the browser, opens the embedded **Browser Terminal**
sidecar when command-line work is needed, and monitors AKS / Storage / ACR / Job
state from a glassmorphic dashboard. The user never opens a local terminal during
steady state; local commands are only for developers or operators bringing up the
control plane itself.

> Project charter: [.github/copilot-instructions.md](./.github/copilot-instructions.md)
> · Agent navigation map: [AGENTS.md](./AGENTS.md)

## Get started

New to this repo? Start with the guided setup: [docs/get-started.md](./docs/get-started.md).

It covers Windows with WSL2, macOS, Linux, exact tool versions, Python 3.12 setup
with `uv`, Node 20 setup for the SPA, local development commands, first Azure
deployment with `azd up`, and the post-deploy redirect URI step for browser sign-in.

## Dashboard preview

![ElasticBLAST Control Plane dashboard](./docs/images/dashboard.png)

A single glance shows every moving part of an ElasticBLAST run on Azure:

- **Azure Kubernetes Service Cluster** — node-pool CPU/memory live, cluster
  state, kubelet object id, and which BLAST databases are pre-warmed on each
  cluster (`16S_ribosomal_RNA 3/3`, `core_nt 0/3`). Start/stop/delete actions
  are inline.
- **Azure Container Registry** — login server, SKU, and the four pinned
  ElasticBLAST images (`elb 1.4.0`, `job-submit 4.1.0`, `query-split 0.1.4`,
  `openapi 3.4`) with build status per image. A one-click **Build** kicks off
  `az acr build` via a Celery task on the worker sidecar.
- **Storage Account** — region, SKU, HNS state, and the read-only
  `publicNetworkAccess` indicator (always **Disabled** in steady state
  — see [docs/container-apps-migration.md §Storage Network Isolation](./docs/container-apps-migration.md#storage-network-isolation-hard-requirement)).
  The container row shows blob counts and last-update times for `blast-db`,
  `queries`, and `results`; the BLAST Databases chip row reflects what is
  ready for immediate use.
- **Browser Terminal** — `terminal` sidecar process state, last `az login`
  heartbeat, and an **Open** button that launches the embedded shell
  (xterm.js over a same-origin WebSocket → loopback `ttyd`). No SSH, no
  password reveal.
- **BLAST Jobs** — submission history with status, elapsed time, and
  drill-down to the Celery task's full event history from Table Storage.
  The card is empty in this screenshot because no jobs were submitted yet.

> Subscription name and the kubelet object id are masked in this screenshot.
> The dashboard renders the real values when you sign in.

## Layout

```
api/    Backend — FastAPI for the api sidecar + Celery worker/beat (also contains shared Azure SDK service wrappers and HTTP boundary helpers)
web/        React + Vite + TypeScript SPA + Dockerfile + nginx.conf for the frontend sidecar
terminal/   Dockerfile + entrypoint for the terminal sidecar (ttyd + elastic-blast toolchain)
infra/      Bicep IaC (network, identity, ACR, storage, Key Vault, Container Apps Env, Container App)
scripts/    Dev helpers + postprovision hook (`postprovision.sh` builds images and swaps the Container App template)
docs/       Architecture notes + per-feature change log
```

## Quick start: deploy to Azure in one command

This repo is hardened so a fresh `git clone` can deploy the full
control-plane bundle (one Container App with six sidecars, a private VNet, a
locked-down Storage account, an ACR, a Key Vault, and Log Analytics) with a
single `azd up`. **Cost is roughly USD 130/month** in `koreacentral` for the
default sizing (see [docs/container-apps-migration.md §Cost Estimate](./docs/container-apps-migration.md#cost-estimate-korea-central-usd-monthly)).

For first-time setup, especially on Windows, use the guided walkthrough first:
[docs/get-started.md](./docs/get-started.md).

### 1. Install prerequisites

```bash
# macOS / Linux
curl -fsSL https://aka.ms/install-azd.sh | bash
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash   # or `brew install azure-cli`
sudo apt install -y jq curl                              # `brew install jq curl`
```

Verify:

```bash
./scripts/dev/preflight-check.sh
```

### 2. Sign in

```bash
az login
az account set --subscription "<your-subscription>"
```

The deployment creates or reuses the App Registration automatically during `azd up`. If your tenant blocks App Registration creation, ask an Entra administrator to create it once and set `API_CLIENT_ID` in the azd environment.

### 3. Create the azd environment

```bash
azd env new elb-dashboard
azd env set AZURE_LOCATION       koreacentral

# Optional: same-origin only (recommended once the SPA is served by the
# frontend sidecar). Leave empty for that.
# azd env set ALLOWED_ORIGINS     "https://my-other-spa.example.com"

# Optional: leave private networking off for the very first deploy so the
# postprovision hook can push images and seed secrets, then flip on a
# second `azd provision`.
azd env set LOCKDOWN_PRIVATE_NETWORKING false
```

### 4. Deploy

For the shortest fresh-clone path, run the bootstrap wrapper. It checks `az account show`, starts `az login` if needed, prepares the `elb-dashboard` azd environment so the resource group is `rg-elb-dashboard`, runs `azd up`, and opens the deployed Container App URL:

```bash
./deploy.sh
```

If you prefer the raw azd command after preparing/selecting the environment, run:

```bash
azd up
```

`azd up` runs:

The command prints an `azd up progress map` before long-running work starts,
then marks the active step as `[n/7]` while it runs.

1. **preprovision** — registers deployment Azure resource providers
  (`Microsoft.App`, `Microsoft.Authorization`, `Microsoft.ContainerRegistry`,
  `Microsoft.Storage`, etc.) and starts first-run workflow provider registration
  for Compute, ContainerService, and Quota.
2. **provision** — runs [infra/main.bicep](./infra/main.bicep) which creates
  the platform RG, VNet (3 subnets), Log Analytics, optional App Insights, the
   shared user-assigned managed identity, the Premium ACR, the
   Standard_LRS Storage account (with state tables / blob containers / two
   Azure Files shares), the Key Vault, the Container Apps Environment, and
   the Container App seeded with a hello-world bootstrap image.
3. **postprovision** — runs [scripts/dev/postprovision.sh](./scripts/dev/postprovision.sh)
   which builds the three images (`elb-api`, `elb-frontend`, `elb-terminal`)
   via `az acr build` (no local Docker needed) and runs `az deployment group
   create` to swap the Container App template to the six-sidecar layout.

When it finishes you get an HTTPS URL like
`https://ca-elb-dashboard.<subdomain>.koreacentral.azurecontainerapps.io`.
`/api/health` should respond `200 {"status": "ok", ...}`.

### 5. (Recommended) Lock down the network on the second deploy

The first deploy keeps Storage / Key Vault / ACR public so `az acr build`
and Key Vault seeding can run from the operator's machine. The second
deploy flips `publicNetworkAccess` to `Disabled` on all three and adds
private endpoints:

```bash
azd env set LOCKDOWN_PRIVATE_NETWORKING true
azd provision
```

After this point the only client that can reach platform Storage / Key
Vault / ACR is the Container App, over private endpoints inside the
platform VNet (see [docs/container-apps-migration.md §Storage Network Isolation](./docs/container-apps-migration.md#storage-network-isolation-hard-requirement)).

### 6. Tear down

```bash
azd down --purge --force
```

Removes the platform RG and purges Key Vault soft-deletes.

---

## Architecture Planning

- [Container Apps architecture reference](./docs/container-apps-migration.md) —
  the **shipped** layout: a single Azure Container App that bundles six sidecars
  (`frontend` nginx serving the React SPA, `api` FastAPI, Celery `worker`,
  Celery `beat`, a `redis` broker, and a `terminal` shell with the
  `elastic-blast` toolchain). State lives in **Azure Storage** (table + append
  blobs); Redis AOF and the terminal `/home/azureuser` are persisted on Azure
  Files shares.
  **Every Storage account is `publicNetworkAccess=Disabled` from day 1** and
  is reachable only by the Container App over private endpoints in the
  platform VNet. **All browser uploads and downloads are streamed through the
  api sidecar — no SAS tokens are issued to the browser.** **The browser
  shell is the `terminal` sidecar; there is no Remote Terminal VM, no SSH,
  and no admin password.** No Service Bus, no managed database, no separate
  Redis VM, no Static Web App, no temporary storage public-access window.

## Prerequisites

| Tool         | Minimum  | Notes                                                                |
| ------------ | -------- | -------------------------------------------------------------------- |
| Azure CLI    | 2.81+    | Run `az login` first                                                 |
| azd          | 1.10+    | `curl -fsSL https://aka.ms/install-azd.sh \| bash`                   |
| uv           | 0.9+     | `curl -LsSf https://astral.sh/uv/install.sh \| sh` — drives Python tooling |
| Python       | 3.12     | Provided by `uv sync` (does not need to be installed system-wide)    |
| Node.js      | 20 LTS   | For the SPA                                                          |
| Docker       | 20.x+    | Optional — only needed for `scripts/dev/docker-compose.local.yml`    |
| jq, curl     | any      | `sudo apt install jq curl`                                           |

Local backend bring-up:

```bash
uv sync --all-groups        # creates .venv on Python 3.12 + installs runtime + dev tools
uv run pytest -q api/tests  # 28 tests
scripts/dev/local-run.sh api
```

VS Code dev tasks and direct terminal runs through `scripts/dev/local-run.sh`
mirror local pipeline logs into `.logs/local/latest/` inside this project. The
newest 3 sessions are retained, each log chunk is capped at 1 MiB, and each
service keeps a bounded 16-chunk ring per session. Start with
`.logs/local/latest/api.log`, then check `worker.log`, `beat.log`, `web.log`,
and `smoke.log` when diagnosing warnings, errors, or pipeline health.
Docker Compose runs should go through `scripts/dev/local-run.sh compose-full`
or `compose-local`; detached compose runs also create
`compose-full-containers.log` / `compose-local-containers.log` with container
stdout/stderr and replay only the newest 200 lines by default.

### Driving a deployed environment from your laptop (one-time RBAC + network)

The local api uses `DefaultAzureCredential` → your `az login` identity, which
starts with **zero** RBAC on the workload Storage / ACR. Without the steps
below the dashboard will render `network_blocked` / `access_denied` and DB
downloads will fail with HTTP 403.

```bash
# 1. one-shot: grant your az user the minimum roles on the deployed environment.
#    Defaults match docs/auth.md (storage=elbstg01 in rg-elb-01, acr=elbacr01 in rg-elbacr-01).
#    Override with --storage / --storage-rg / --acr / --acr-rg if your deployment differs.
scripts/dev/grant-local-rbac.sh                  # add --dry-run to preview
# wait 1-5 min for RBAC propagation, then:

# 2. start the api with the local-debug Storage auto-open helper enabled —
#    this opens publicNetworkAccess for your caller IP only when needed.
LOCAL_DEBUG_AUTO_OPEN_STORAGE=true \
  AUTH_DEV_BYPASS=true \
  scripts/dev/local-run.sh api

# 3. when you're done debugging, close the network surface again:
scripts/dev/storage-public-access.sh off
```

Both helpers are idempotent; both refuse to act inside a Container App
(`CONTAINER_APP_NAME` env present). See
[`scripts/dev/README.md`](./scripts/dev/README.md) and
[`.github/copilot-instructions.md`](./.github/copilot-instructions.md) §9.

---

## Roadmap

The following are deliberately **not** in scope for the current milestone
and would be addressed in a follow-up PR:

- Wire the streaming upload/download proxy through to the SPA's `BlastResults`
  download/export buttons (the routes today return 503 `streaming_proxy_pending`
  by design).
- Implement the remaining Celery tasks for `aks/openapi/deploy`, `acr/build`,
  `storage/prepare-db`, and `blast/submit|delete|warmup`; today their HTTP
  surfaces are stubs that return `streaming_proxy_pending`-style 503s.
- CI pipeline for `azd up` against an ephemeral subscription.

## Authentication (production path)

In production (`AUTH_DEV_BYPASS=false`):

1. SPA acquires an MSAL access token for `api://<client-id>/user_impersonation`.
2. The api sidecar validates the JWT against the tenant's OIDC discovery + JWKS.
3. Backend uses the shared user-assigned Managed Identity `id-elb-dashboard-*` (mounted on the Container App, visible to all sidecars) for downstream ARM and data-plane calls. The browser token proves who called; it is not exchanged for Azure resource tokens.
4. The `terminal` sidecar inherits the same MI — `az login --identity` works out of the box. Device-code login is only needed when a user intentionally wants a personal Azure CLI session.

`AUTH_DEV_BYPASS=true` short-circuits step 2 and lets the API call Azure
with whatever credential `DefaultAzureCredential` finds (typically your
local `az login`). **Never enable this in production.**
