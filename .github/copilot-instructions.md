# elb-dashboard — Copilot Instructions

> **Sibling repository**: [`dotnetpower/elastic-blast-azure`](https://github.com/dotnetpower/elastic-blast-azure) — the BLAST runtime this project automates.
> Local clone (always read-only from this repo's point of view): `~/dev/elastic-blast-azure`.
> Authoritative setup reference: `~/dev/elastic-blast-azure/docs/azure-prereq.md`.

---

## 0. Implementation Discipline (NON-NEGOTIABLE)

**Do NOT rush to implement.** Before writing any code:

1. **Step back** — Read the request carefully. Identify what is actually being asked.
2. **Deep analysis** — Investigate the current codebase, data flow, Azure API behaviour, and edge cases. Run exploratory commands and read existing code before making assumptions.
3. **Plan** — Write a concrete plan (what files change, what APIs are called, what the data flow looks like, what can go wrong). Present the plan for review if the scope is non-trivial.
4. **Implement** — Only after the plan is solid. Make one logical change at a time, verify each step.
5. **Verify** — Test the change end-to-end (curl, browser, or pytest). Never mark done without evidence of success.

> Fast coding that breaks things costs more than thoughtful coding that works the first time. When in doubt, investigate more before typing.

---

## 1. Mission

Provide a **browser-only** control plane for ElasticBLAST on Azure so a researcher never opens a local terminal:

1. **Web UI** (glassmorphic, calm/muted theme) hosts every action. Served by the `frontend` (nginx) sidecar in the bundled Container App.
2. **Browser Terminal** is a `terminal` sidecar in the same Container App revision, exposed to the browser via xterm.js → WebSocket → loopback `ttyd` (proxied by the `api` sidecar after MSAL + role check). The `elastic-blast` CLI runs there, not on a VM.
3. The UI continuously **monitors** the AKS cluster, Storage Account / databases, ACR images, and ElasticBLAST job state. Long-running work (BLAST submit/delete, ACR builds, AKS provisioning, DB warmup, schedules) is dispatched to the `worker` (Celery) sidecar via the in-revision `redis` broker; the `beat` sidecar handles periodic schedules.
4. **Authentication is interactive `az login`** in the browser. No service principal secrets, no client credentials flow.

> If a feature cannot be driven from the browser, it is out of scope for this repo. The user must never be asked to "run this command locally".

---

## 2. Language Policy (NON-NEGOTIABLE)

* **Conversation with the user**: Korean (the user's preferred language).
* **Everything else is English**: source code, identifiers, comments, docstrings, log messages, commit messages, branch names, PR titles, README/docs, UI labels & toasts, error strings, configuration keys, file names.
* No mixed-language strings. No Korean inside `*.py`, `*.ts`, `*.tsx`, `*.bicep`, `*.md`, `*.json`, `*.yaml`.
* If a translation table is ever needed for the UI, English is the source of truth and the only locale shipped initially.

---

## 3. Stack & Versions (pinned)

> **Migration status (2026-05)**: The control plane has moved from Azure Functions to **Azure Container Apps**. The new backend lives in `api/` (FastAPI + Celery). The old `api/` tree (Azure Functions v2 + Durable Functions) is **legacy** — still in the repo for reference but is no longer the deploy target. New work goes into `api/`. See [docs/container-apps-migration.md](../docs/container-apps-migration.md) for the full target architecture.

| Layer            | Choice                                                                       | Reason                                                                |
| ---------------- | ---------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| Hosting          | **Azure Container Apps** — single Container App `ca-elb-control` with six sidecars (`frontend`, `api`, `worker`, `beat`, `redis`, `terminal`); pinned `minReplicas: 1`, `maxReplicas: 1` | One billable revision, private VNet, queue-backed workers, sidecar terminal. No Function App, no Static Web App, no Remote Terminal VM. |
| API              | **FastAPI on Python 3.12** (`api/`), served by uvicorn in the `api` sidecar on `:8080` | Long-running operations, WebSocket terminal proxy, streaming upload/download proxy that don't fit the Functions HTTP model. |
| Long-running     | **Celery 5 worker + beat sidecars**, broker = in-revision `redis:7-alpine` sidecar with AOF on Azure Files | Replaces Durable Functions. BLAST submit/delete, ACR builds, AKS provisioning, DB warmup, scheduled work. |
| Durable state    | **Azure Storage** — Table for job/audit/schedule rows, append blobs for command history; **no managed database** | Cost-minimised; documents/append workloads only. |
| Frontend         | **React + Vite + TypeScript** (`web/`), built into `dist/` and served by the `frontend` (nginx:alpine) sidecar at `127.0.0.1:8081`; the `api` sidecar reverse-proxies non-`/api/*` requests to it | Same-origin, no SWA resource. |
| Browser auth     | **MSAL.js (`@azure/msal-browser`) → Auth Code + PKCE**                       | Mirrors `az login` UX; backend validates the bearer token.            |
| Backend auth     | **`azure-identity` `DefaultAzureCredential`** using the user-assigned MI `id-elb-control` (shared by all six sidecars) | All Azure SDK calls use MI; bearer token is for identity verification only. |
| Browser terminal | **xterm.js + WebSocket → loopback `ttyd` in the `terminal` sidecar** (no SSH, no VM, no admin password) | The `terminal` sidecar carries the `elastic-blast` toolchain; `/home/azureuser` is persisted on an Azure Files share. |
| Data plane       | **All browser uploads/downloads stream through the `api` sidecar** (1 MiB chunks, 4 MiB block uploads, semaphore-capped to 4 concurrent transfers) — **never issue SAS tokens to the browser** | Storage stays `publicNetworkAccess: Disabled`; only the Container App reaches it (via private endpoints). |
| IaC              | **Bicep** (`infra/`); legacy Function App + SWA Bicep preserved under `legacy/infra/` for reference | Idiomatic for Container Apps Environment / private endpoints / Vault. |
| Deploy tooling   | **Azure Developer CLI (`azd`)** + `postprovision.sh` that runs `az acr build` and swaps the Container App template to the six-sidecar layout | Single `azd up` from a fresh clone, no local Docker needed.           |
| Secrets          | **Azure Key Vault** (App Registration values, etc.)                          | Never store secrets in env vars committed to the repo.                |

Pin Azure CLI ≥ 2.81, kubectl ≥ 1.34, azcopy ≥ 10.28, BLAST+ 2.17.0 — same versions validated by `elastic-blast-azure` on 2026-04-29.

---

## 4. Repository Layout

Create directories on demand; do not scaffold empty folders speculatively.

```
.
├── api/                     # Backend — FastAPI for the `api` sidecar + Celery worker/beat
│   ├── main.py                  # FastAPI app entrypoint (uvicorn target)
│   ├── celery_app.py            # Celery app + queue routing
│   ├── auth.py                  # MSAL bearer token validation
│   ├── _http_utils.py           # Shared HTTP boundary helpers
│   ├── routes/                  # FastAPI routers (arm, monitor, resources, terminal_ws, frontend_proxy, …)
│   ├── services/                # Pure-Python wrappers (azure_clients, monitoring, state_repo, sanitise, image_tags, …)
│   ├── tasks/                   # Celery task modules (BLAST submit/delete, ACR build, AKS provision, schedules)
│   ├── tests/                   # pytest (FastAPI + Celery + shared service modules)
│   ├── Dockerfile               # Image used by both `api` and `worker`/`beat` sidecars
│   └── requirements.txt
├── web/                         # React + Vite + TypeScript SPA + Dockerfile + nginx.conf for the `frontend` sidecar
│   ├── src/
│   │   ├── components/          # Glassmorphic UI building blocks
│   │   ├── pages/               # Dashboard, BrowserTerminal, JobDetail, …
│   │   ├── hooks/
│   │   ├── api/                 # Typed fetchers for /api routes
│   │   └── theme/               # Glassmorphism tokens (CSS variables)
│   ├── nginx.conf               # nginx config for the `frontend` sidecar
│   └── vite.config.ts
├── terminal/                    # Dockerfile + entrypoint for the `terminal` sidecar (ttyd + elastic-blast toolchain)
├── infra/                       # Bicep modules + main.bicep
│   ├── main.bicep               # Container Apps Environment + ca-elb-control + private networking
│   └── modules/                 # containerApp.bicep, network.bicep, identity.bicep, acr.bicep, storage.bicep, keyVault.bicep, …
├── legacy/                      # Retired artefacts kept for reference only — DO NOT add or modify code here
│   ├── functionapp/             # Old Azure Functions v2 backend (function_app.py, orchestrators/, activities/, entities/, routes/, auth/, models/, tests/)
│   ├── infra/                   # Old Function App + Static Web App Bicep
│   └── cloud-init/              # remote-terminal.yaml from the retired Remote Terminal VM model
├── scripts/
│   └── dev/                     # Local dev helpers + postprovision.sh (runs `az acr build` and swaps the Container App template)
├── docs/
│   ├── auth.md
│   ├── container-apps-migration.md  # Authoritative target architecture
│   └── features_change/         # Per-change notes (see §13)
├── tests/                       # Cross-cutting tests; per-component tests live next to their code
├── azure.yaml                   # azd manifest (Bicep provider + pre/postprovision hooks)
└── README.md
```

---

## 5. Authentication Flow

1. SPA loads → MSAL acquires an **ID token + access token** for the app's API audience via Auth Code + PKCE.
2. SPA calls `/api/*` with `Authorization: Bearer <access_token>`.
3. The `api` sidecar (FastAPI) validates the JWT (issuer, audience, signing keys cached from the tenant's OpenID metadata) **before** any business logic runs. Reject all unauthenticated requests with 401.
4. For ARM and data-plane calls, the backend uses the **shared user-assigned Managed Identity** `id-elb-control` (mounted on the Container App and visible to all six sidecars) via `DefaultAzureCredential`. The bearer token is used only for identity verification (who is calling), not for downstream Azure calls. This avoids OBO consent issues and removes the need for `API_CLIENT_SECRET`.
5. The MI must be pre-granted sufficient RBAC roles (see `docs/auth.md` §1 for the full matrix). Runtime role assignments (e.g. granting AcrPull to AKS kubelet) are best-effort — if the MI lacks `User Access Administrator`, the code logs a one-line `az role assignment create` recovery hint instead of failing.
6. The `terminal` sidecar never holds a long-lived Azure credential. The user runs `az login --use-device-code` *inside the browser terminal session* the first time they connect. The resulting `~/.azure/` profile is persisted on the `terminal-home` Azure Files share so subsequent revisions keep the login.

> **Design choice**: We intentionally use Managed Identity instead of OBO. OBO requires `API_CLIENT_SECRET` and multi-resource consent, which are fragile in single-tenant research environments. MI simplifies deployment at the cost of the MI needing broad permissions — acceptable because the MI is scoped to the Container App and auditable via Azure Monitor.

---

## 6. Browser Terminal — Sidecar Lifecycle

The Browser Terminal is the `terminal` sidecar in the `ca-elb-control` Container App. It carries the `elastic-blast` toolchain and is reached from the SPA via xterm.js → WebSocket → loopback `ttyd`. **There is no Remote Terminal VM, no SSH, no admin password, no NSG, no public IP.** Anything in `legacy/functionapp/orchestrators/provision_terminal.py` and `legacy/cloud-init/remote-terminal.yaml` belongs to the retired model and is kept for reference only.

### 6.1 Image (`terminal/Dockerfile`)

The `terminal` image is built by `az acr build` during `postprovision.sh`. It must:

* Be Ubuntu-based and install `azure-cli` ≥ 2.81, `kubectl` ≥ 1.34, `azcopy` ≥ 10.28, Python 3.12 + `python3.12-venv`, `git`, `make`, `jq`, `unzip`, `curl`, `tmux`, and `ttyd`.
* Clone `https://github.com/dotnetpower/elastic-blast-azure.git` into `/opt/elastic-blast-azure` at build time and `pip install` its `requirements/test.txt` into a venv that is on PATH for the operator.
* Default `ENTRYPOINT` runs `ttyd` bound to **127.0.0.1** only (the `api` sidecar is the only client; never expose `ttyd` on the public ingress).
* Set `~/.bashrc` to export `PYTHONPATH=src:$PYTHONPATH`, `AZCOPY_AUTO_LOGIN_TYPE=AZCLI`, `ELB_SKIP_DB_VERIFY=true`, `ELB_DISABLE_AUTO_SHUTDOWN=1`, and write a MOTD telling the user the next step is `az login --use-device-code`.

### 6.2 Persistence

`/home/azureuser` is mounted from the `terminal-home` Azure Files share. That keeps the `~/.azure/` profile, kubeconfig, ssh known_hosts, and any staged query files across revisions and restarts.

### 6.3 Browser path

* The SPA page (e.g. `BrowserTerminal`) opens a WebSocket to `/api/terminal/ws` on the `api` sidecar.
* The `api` sidecar validates the bearer token + role, then proxies the WebSocket to `127.0.0.1:7681` inside the `terminal` sidecar.
* No download, no SSH client, no password reveal. Display "Run `az login --use-device-code` first" as a one-time helper banner.

### 6.4 Lifecycle controls

There is no "Destroy Remote Terminal" action because there is no VM. The lifecycle controls reduce to:

* **Restart terminal** — restart the `terminal` sidecar process (`ttyd`) without rolling the revision.
* **Reset home** — clear `/home/azureuser` on the Files share (must require explicit confirmation; this drops the cached `az login`).

---

## 7. ElasticBLAST Resource Plane (driven by the backend, not the terminal)

The web app is the source of truth for the *infrastructure* the elastic-blast CLI talks to. Implement these as **Celery tasks** in `api/tasks/` (queued onto the in-revision Redis broker, executed by the `worker` sidecar). The `api` sidecar enqueues the task and returns a task id; the SPA polls `/api/tasks/<id>` for progress. State (status, history, audit) lives in Azure Table Storage via `api/services/state_repo.py`.

| Celery task              | Mirrors azure-prereq.md | Notes                                                                                                                       |
| ------------------------ | ----------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `ensure_resource_groups` | Step 3                  | Two RGs: `rg-elb` (workload) and `rg-elbacr` (registry). Both names + region configurable in the UI.                        |
| `ensure_acr`             | Step 4                  | Standard SKU. Idempotent. Output: login server.                                                                              |
| `build_acr_images`       | Step 6                  | Use **`az acr build` REST API** (no local Docker). Build `ncbi/elb:1.4.0`, `ncbi/elasticblast-job-submit:4.1.0`, `ncbi/elasticblast-query-split:0.1.4`. Report per-image status. |
| `ensure_storage`         | Step 7                  | HNS-enabled `Standard_LRS`. Containers `blast-db`, `queries`, `results`. **Reachable from the Container App via private endpoint only** — see §9. |
| `monitor_aks`            | Step 9                  | Polls `aks list/show`, surfaces `provisioningState`, node count, `powerState`, kubelet identity, role assignments. Scheduled by `beat`.          |
| `monitor_jobs`           | Step 9.3                | Polls `kubectl get jobs/pods` via the kubelet API or via the `terminal` sidecar's loopback shell. Persists history to Table Storage. Scheduled by `beat`. |

Tasks must be **idempotent** (Celery retries on transient failures), **side-effect-tagged** in the docstring, and write progress checkpoints to the state repo so the UI shows real progress instead of a spinner.

Image tags MUST stay in sync with `src/elastic_blast/constants.py` in the sibling repo. Regression check: every task that builds images reads tag values from a single `IMAGE_TAGS` dict (`api/services/image_tags.py`) that future contributors can update in one place. Hard-code today's pinned tags; re-validate when bumping.

---

## 8. Monitoring UI (primary surface)

The dashboard is the landing page; the Browser Terminal is one tab among many.

Required cards (each backed by a polled REST endpoint, 30 s default refresh):

1. **Cluster** — AKS name, RG, region, K8s version, node pool size/SKU, `powerState`, `provisioningState`, kubelet identity object id, attached ACR.
2. **Storage** — account name, region, public-access state (read-only indicator; should always show **Disabled** for the workload account), container list, blob counts/sizes for `blast-db/`, `queries/`, `results/`.
3. **ACR** — registry, login server, repositories with tag table (highlight mismatches against `IMAGE_TAGS`).
4. **Jobs** — list of ElasticBLAST submissions with status (`Provisioning | Downloading DB | Splitting | Running | Completed | Failed | Deleted`), elapsed time, results URL. Drill-down opens the Celery task's full event history from Table Storage.
5. **Browser Terminal** — `terminal` sidecar process state, last `az login` heartbeat (mtime of `~/.azure/azureProfile.json` on the `terminal-home` share), button to open the embedded shell.
6. **Container App** — revision name, image digests for each sidecar, replica count (always 1), CPU/memory % per sidecar pulled from App Insights.

All numbers must come from real Azure / Kubernetes APIs. Never fabricate or cache stale data without showing a "last refreshed" timestamp.

---

## 9. Storage Network Isolation (HARD REQUIREMENT)

**Production posture is `publicNetworkAccess: Disabled` on every workload Storage account, period.** The deployed Container App reaches platform Storage exclusively over private endpoints from inside the platform VNet. There is **no production code path that flips this on**, no `bypass: AzureServices` workaround for production traffic.

**Local-debug exception (manual, IP-allowlist only).** A developer iterating from a laptop cannot reach the data plane through the private endpoint, so the BLAST Databases / Queries / Results screens render the `network_blocked` degraded state. To exercise those code paths locally, run [scripts/dev/storage-public-access.sh](../scripts/dev/storage-public-access.sh):

```
scripts/dev/storage-public-access.sh on   # publicNetworkAccess=Enabled, defaultAction=Deny, ipRules=[<your IP>]
# ... debug ...
scripts/dev/storage-public-access.sh off  # back to publicNetworkAccess=Disabled
```

This is intentionally a manual shell command, not a dashboard button or environment toggle — the friction is the safety mechanism. RBAC (`Storage Blob Data Reader` / `Contributor`) is unchanged and still enforced; the script only opens the network surface to the caller's public IP. Do not check in any wrapper that calls this without explicit confirmation, and do not leave the surface open after debugging.

Consequences for the code:

* All browser uploads/downloads of queries/results are **streamed through the `api` sidecar** (1 MiB chunks for download, 4 MiB block uploads, semaphore-capped to 4 concurrent transfers). This stays true regardless of the local-debug toggle state.
* **Never** issue SAS tokens to the browser. Never return a Storage URL the browser is expected to fetch directly.
* `elastic-blast` itself runs inside the `terminal` sidecar (same VNet) and reaches Storage via the same private endpoint, so the historical `publicNetworkAccess=Enabled` requirement during `submit/status/delete` does **not** apply here.
* The dashboard's Storage card surfaces the `publicNetworkAccess` value. **Enabled with `defaultAction=Deny` + a non-empty `ipRules` is an acceptable local-debug state**, but `Enabled` with `defaultAction=Allow`, or `Enabled` left over after debugging in a deployed environment, is an incident — remediate by running `storage-public-access.sh off`.

---

## 10. Glassmorphic UI — Design Rules

Calm, muted, low-contrast surfaces. Reference tokens (use as CSS variables in `web/src/theme/`):

```css
:root {
  --glass-bg: rgba(255, 255, 255, 0.08);
  --glass-bg-strong: rgba(255, 255, 255, 0.14);
  --glass-border: rgba(255, 255, 255, 0.18);
  --glass-blur: 18px;
  --glass-radius: 16px;
  --bg-gradient: radial-gradient(1200px 600px at 20% 0%, #1c2541 0%, #0b132b 60%, #050816 100%);
  --text-primary: #e8ecf4;
  --text-muted:   #9aa3b8;
  --accent:       #7aa7ff;   /* cool, low-saturation blue */
  --success:      #6ad6a3;
  --warning:      #f0c674;
  --danger:       #e07b8a;
}

.glass-card {
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  border-radius: var(--glass-radius);
  backdrop-filter: blur(var(--glass-blur));
  -webkit-backdrop-filter: blur(var(--glass-blur));
  box-shadow: 0 8px 32px rgba(0,0,0,0.25);
}
```

* Avoid pure black, pure white, and saturated brand colors. Stay in the deep-navy / cool-grey family.
* No drop shadows above 32 px blur, no neon, no animated gradients.
* Motion: `prefers-reduced-motion` respected; transitions ≤ 200 ms ease-out.
* Iconography: `lucide-react`, stroke 1.5.
* Components must be readable on a 1366×768 laptop and accessible (WCAG AA contrast on text against the glass surface).

---

## 11. Coding Standards

### Python (api/)

* Python **3.12** (pinned in `.python-version`, `pyproject.toml` `requires-python = ">=3.12,<3.13"`, and the api / terminal Dockerfiles).
* **Package management is `uv` only.** `pyproject.toml` carries the dependency list; `uv.lock` is the source of truth. Never check in a `requirements.txt`, never `pip install` outside a Dockerfile, never edit `uv.lock` by hand.
  * Local dev: `uv sync --all-groups` creates `.venv/` and installs everything (runtime + dev tools).
  * Adding a runtime dep: edit `[project].dependencies` then `uv lock --upgrade-package <name>`; commit `pyproject.toml` + `uv.lock` together.
  * Adding a dev-only tool: edit `[dependency-groups].dev` then `uv lock`.
  * Run anything from the venv with `uv run <cmd>` (e.g. `uv run uvicorn …`, `uv run pytest …`, `uv run ruff check`).
* New code goes in `api/`. The retired Azure Functions tree under `legacy/functionapp/` is reference only — no edits, no bug-fix backports, no imports from there.
* Format with `ruff format`, lint with `ruff check`. No `black`/`isort` duplication.
* Type hints required on all public functions; `mypy --strict` clean.
* Pydantic v2 for request/response models; never accept untyped `dict` at HTTP boundaries.
* Azure SDK calls go through `api/services/` wrappers — FastAPI routes and Celery tasks must not import `azure.mgmt.*` directly.
* **Never use Azure Run Command** (`ManagedClusters.begin_run_command`, `VirtualMachines.begin_run_command`). Both are ~30 s slow and ARM-rate-limited. For Kubernetes operations use the existing `api.services.monitoring.k8s_*` helpers (direct K8s API via the kubeconfig token) — add a new `k8s_*` function if needed. For genuinely shell-only work (`azcopy`, `elastic-blast` CLI, `kubectl exec`, `az`) call `api.services.terminal_exec.run()` / `.stream()`; that helper POSTs to a stdlib HTTP server in the `terminal` sidecar (loopback `127.0.0.1:7682`) authenticated by the `exec-token` Container Apps secret, with `argv[0]` allowlisted to `{azcopy, kubectl, elastic-blast, elb, az}` and concurrency capped at `EXEC_MAX_CONCURRENCY`. The api / worker images intentionally do not ship those CLIs — they only live in the `terminal` sidecar.
* No `print` — use the standard `logging` module; structured logs (JSON) preferred. The repo's existing `RequestIdMiddleware` already emits a one-line completion record per request — keep that contract.
* Celery tasks are **idempotent** and **side-effect-tagged** in the docstring; long tasks must write progress checkpoints to `state_repo` so the UI can render real progress.

### TypeScript (web/)

* `eslint` + `prettier`, `strict: true` in `tsconfig.json`.
* React function components only. Hooks for state; **no Redux** unless three independent reviewers agree.
* All `/api` calls go through generated typed clients in `src/api/` — no raw `fetch` in components.
* Use TanStack Query for polling/caching the monitoring endpoints.

### General

* Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`…). English only.
* No commented-out code in commits. Delete it; git remembers.
* No new dependency without justification in the PR description.

---

## 12. Security Checklist (apply to every PR)

* [ ] No secrets in source, `.env`, or fixture files. Use Key Vault references.
* [ ] Every FastAPI route validates the MSAL bearer token before doing work (and the WebSocket handshake for `/api/terminal/ws` does the same — reject the upgrade if the token is missing/invalid).
* [ ] ARM and data-plane calls use the shared user-assigned MI `id-elb-control` via `DefaultAzureCredential`. Do not introduce client secrets or OBO flows.
* [ ] **Every Storage account remains `publicNetworkAccess: Disabled`.** No code path enables it, even temporarily. Browser uploads/downloads stream through the `api` sidecar; no SAS tokens are issued to the browser.
* [ ] `ttyd` in the `terminal` sidecar binds to **127.0.0.1 only**. The Container App's public ingress targets the `api` sidecar on `:8080`; the terminal must never be reachable directly from the internet.
* [ ] Output of `az`/`kubectl`/Celery results shown in the UI is sanitised — never echo tokens, subscription IDs, or full SAS URLs.
* [ ] Bicep deployments carry the standard tag set (`azd-env-name`, `app`, `environment`, `costCenter`, `managedBy`, `repo`, `topology`, optional `owner`) computed in [infra/main.bicep](../infra/main.bicep) `var tags`, plus a per-module `role:` tag (`registry`, `control-plane`, `secrets`, …) merged in via `union(tags, { role: '…' })` inside each [infra/modules/*.bicep](../infra/modules/) — when adding a new module, follow the same pattern.

---

## 13. Process Discipline

### Per-feature change notes
Before each commit that adds or alters user-visible behaviour, create:

```
docs/features_change/YYYY-MM/YYYY-MM-DD-<short-name>.md
```

containing: motivation, user-facing change, API/IaC diff summary, validation evidence (screenshot, curl, or test name).

### Validation before marking done
* Backend changes (`api/`): `uv run pytest -q api/tests` + a local smoke test (`uv run uvicorn api.main:app --reload` for HTTP routes; `uv run celery -A api.celery_app worker -l info` for task changes). Curl the new route or trigger the new task with evidence in the change note.
* Frontend changes: `npm run build` (in `web/`) + screenshot of the affected page.
* Infra changes: `az deployment sub what-if` (or `azd provision --preview`) output attached to the change note. For the bundled Container App, also confirm `postprovision.sh` still applies the six-sidecar template diff cleanly.
* **Do not** rely on `func start` for new work — the `legacy/functionapp/` tree is reference only and not part of the deploy pipeline.

### Cross-repo consistency
When `dotnetpower/elastic-blast-azure` updates `src/elastic_blast/constants.py` image tags or the `azure-prereq.md` step structure, open a tracking issue here and bump `IMAGE_TAGS` / cloud-init in the same PR.

---

## 14. Out of Scope (explicit)

* Anything that requires the user to run a command on their own laptop.
* AWS / GCP code paths (the upstream supports them; this control plane is Azure-only).
* Multi-tenant SaaS hosting — assume one Azure tenant per deployment.
* Storing per-user state outside the user's own Azure subscription.
* **Any new Azure Functions or Durable Functions code.** The Functions runtime is retired; new work goes into `api/` (FastAPI + Celery).
* **Any new Remote Terminal VM, NSG, public IP, SSH path, or admin password handling.** The browser terminal is a sidecar.
* **Re-introducing a managed database (Cosmos DB / PostgreSQL / managed Redis), Service Bus, or Static Web App.** State lives in Azure Storage; the broker is the in-revision Redis sidecar; the SPA is served by the `frontend` sidecar.

---

## 15. Quick Reference — Where Things Live

> For the *map* (file:line anchors, route table, agent tripwires, validation
> cheatsheet) see [AGENTS.md](../AGENTS.md). The table below is a thin
> surface index; AGENTS.md has the deep links and the load-bearing mistakes
> list.

| Need to…                                  | Edit                                                |
| ----------------------------------------- | --------------------------------------------------- |
| Add a new monitoring card                 | `web/src/pages/Dashboard.tsx` + a new route in `api/routes/monitor.py` |
| Add a new HTTP route                      | `api/routes/<area>.py` + register in `api/main.py` |
| Add a new long-running operation          | `api/tasks/<area>.py` (Celery task) + an enqueue endpoint in `api/routes/` |
| Change tools installed in the terminal    | `terminal/Dockerfile` + `terminal/entrypoint.sh`    |
| Bump pinned ACR image tags                | `api/services/image_tags.py` (`IMAGE_TAGS` dict) |
| Adjust glass styling                      | `web/src/theme/glass.css`                           |
| Add a new Bicep resource                  | `infra/modules/*.bicep` + wire into `infra/main.bicep` |
| Change Container App sidecar layout       | `infra/modules/containerApp.bicep` (or the template diff applied by `scripts/dev/postprovision.sh`) |
| Document a behaviour change               | `docs/features_change/YYYY-MM/…md` (mandatory)      |
