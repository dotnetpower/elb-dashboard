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
| IaC              | **Bicep** (`infra/`)                                                         | Idiomatic for Container Apps Environment / private endpoints / Vault. |
| Deploy tooling   | **Azure Developer CLI (`azd`)** + `postprovision.sh` that runs `az acr build` and swaps the Container App template to the six-sidecar layout | Single `azd up` from a fresh clone, no local Docker needed.           |
| Secrets          | **Azure Key Vault** (App Registration values, etc.)                          | Never store secrets in env vars committed to the repo.                |

Pin Azure CLI ≥ 2.81, kubectl ≥ 1.34, azcopy ≥ 10.28, BLAST+ 2.17.0 — same versions validated by `elastic-blast-azure` on 2026-04-29.

---

## 4. Repository Layout, Auth, Terminal, Resource Plane, Monitoring UI, Glass UI

These reference sections were moved out of the always-loaded charter to reduce prompt size. Read on demand:

* [docs/copilot/repo-layout.md](../docs/copilot/repo-layout.md) — full directory tree + "where to edit" table
* [docs/copilot/auth-flow.md](../docs/copilot/auth-flow.md) — MSAL + Managed Identity flow (formerly §5)
* [docs/copilot/browser-terminal.md](../docs/copilot/browser-terminal.md) — `terminal` sidecar lifecycle, image, persistence (formerly §6)
* [docs/copilot/resource-plane.md](../docs/copilot/resource-plane.md) — Celery task table mirroring `azure-prereq.md` (formerly §7)
* [docs/copilot/monitoring-ui.md](../docs/copilot/monitoring-ui.md) — dashboard card spec (formerly §8)
* [docs/copilot/glass-ui.md](../docs/copilot/glass-ui.md) — glassmorphism CSS tokens (formerly §10)

The map for *where* code lives (route table, tripwires) is in [AGENTS.md](../AGENTS.md).

---

## 9. Storage Network Isolation (HARD REQUIREMENT)

**Production posture is `publicNetworkAccess: Disabled` on every workload Storage account, period.** The deployed Container App reaches platform Storage exclusively over private endpoints from inside the platform VNet. There is **no production code path that flips this on**, no `bypass: AzureServices` workaround for production traffic.

**Local-debug exception (explicit, IP-allowlist only).** A developer iterating from a laptop cannot reach the data plane through the private endpoint, so the BLAST Databases / Queries / Results screens render the `network_blocked` degraded state. To exercise those code paths locally, run [scripts/dev/storage-public-access.sh](../scripts/dev/storage-public-access.sh), or the equivalent [scripts/dev/local-run.sh](../scripts/dev/local-run.sh) wrapper commands:

```
scripts/dev/storage-public-access.sh on   # publicNetworkAccess=Enabled, defaultAction=Deny, ipRules=[<your IP>]
scripts/dev/local-run.sh storage-on       # same, using ELB_LOCAL_STORAGE_ACCOUNT / ELB_LOCAL_STORAGE_RG defaults
# ... debug ...
scripts/dev/storage-public-access.sh off  # back to publicNetworkAccess=Disabled
scripts/dev/local-run.sh storage-off      # same via local-run
```

This is intentionally an explicit local-debug action, not a production dashboard control — the friction is the safety mechanism. RBAC (`Storage Blob Data Reader` / `Contributor`) is unchanged and still enforced; the script only opens the network surface to the caller's public IP. The local backend may also call `api.services.storage_public_access.ensure_local_storage_access()` when `LOCAL_DEBUG_AUTO_OPEN_STORAGE=true` and a route has full Storage ARM scope; that helper must keep the `CONTAINER_APP_NAME` guard so deployed Container Apps can never flip Storage open. Do not check in any wrapper that calls this outside local debugging, and do not leave the surface open after debugging.

Consequences for the code:

* All browser uploads/downloads of queries/results are **streamed through the `api` sidecar** (1 MiB chunks for download, 4 MiB block uploads, semaphore-capped to 4 concurrent transfers). This stays true regardless of the local-debug toggle state.
* **Never** issue SAS tokens to the browser. Never return a Storage URL the browser is expected to fetch directly.
* `elastic-blast` itself runs inside the `terminal` sidecar (same VNet) and reaches Storage via the same private endpoint, so the historical `publicNetworkAccess=Enabled` requirement during `submit/status/delete` does **not** apply here.
* The dashboard's Storage card surfaces the `publicNetworkAccess` value. **Enabled with `defaultAction=Deny` + a non-empty `ipRules` is an acceptable local-debug state**, but `Enabled` with `defaultAction=Allow`, or `Enabled` left over after debugging in a deployed environment, is an incident — remediate by running `storage-public-access.sh off`.

---

## 10. Glassmorphic UI — Design Rules

Calm, muted, low-contrast surfaces. **Detail moved to [docs/copilot/glass-ui.md](../docs/copilot/glass-ui.md)** (CSS tokens, `.glass-card` template, accessibility rules). The non-negotiables:

* Stay in the deep-navy / cool-grey family. Avoid pure black/white and saturated brand colors.
* No drop shadows above 32 px blur, no neon, no animated gradients. Transitions ≤ 200 ms ease-out.
* `lucide-react` icons (stroke 1.5). WCAG AA contrast on text against the glass surface.

---

## 11. Coding Standards

### Python (api/)

* Python **3.12** (pinned in `.python-version`, `pyproject.toml` `requires-python = ">=3.12,<3.13"`, and the api / terminal Dockerfiles).
* **Package management is `uv` only.** `pyproject.toml` carries the dependency list; `uv.lock` is the source of truth. Never check in a `requirements.txt`, never `pip install` outside a Dockerfile, never edit `uv.lock` by hand.
  * Local dev: `uv sync --all-groups` creates `.venv/` and installs everything (runtime + dev tools).
  * Adding a runtime dep: edit `[project].dependencies` then `uv lock --upgrade-package <name>`; commit `pyproject.toml` + `uv.lock` together.
  * Adding a dev-only tool: edit `[dependency-groups].dev` then `uv lock`.
  * Run anything from the venv with `uv run <cmd>` (e.g. `uv run uvicorn …`, `uv run pytest …`, `uv run ruff check`).
* New code goes in `api/`. The Azure Functions tree was deleted from the repository on 2026-05-19 — do not try to re-create it under `legacy/`.
* Every new Python file (`api/`, `terminal/`, `scripts/dev/`, `web/*.py`, and tests)
  must start with a natural module docstring context header. Do **not** use a literal
  `AI Context Header.` label. The first line is a concise module summary, followed
  by these fields: `Responsibility`, `Edit boundaries`, `Key entry points`,
  `Risky contracts`, and `Validation`. Keep it synchronized with the actual code
  whenever entry points, contracts, or validation commands change.
* Use the context header as an SRP gate. If the `Responsibility` line needs "and"
  chains, unrelated nouns, or more than one architectural layer (route + service +
  task + parser), split the work before adding more code. Routes own HTTP/auth/
  response shaping; services own reusable domain/Azure/Kubernetes/Storage logic;
  tasks own long-running side effects and progress checkpoints; tests own one
  behaviour family. When editing a large module, prefer adding a focused helper
  module over broadening the existing header.
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

### GitHub issue closure hygiene
When work is tied to a registered GitHub issue, do not leave the issue silent. Before marking the task done, verify the issue's acceptance criteria against the implemented diff and validation evidence. If the criteria are met, add an issue comment summarising the shipped change and validation, then close the issue. If anything remains, leave the issue open and comment with the completed work, validation evidence, and explicit remaining gap.

### Validation before marking done
* Backend changes (`api/`): `uv run pytest -q api/tests` + a local smoke test (`uv run uvicorn api.main:app --reload` for HTTP routes; `uv run celery -A api.celery_app worker -l info` for task changes). Curl the new route or trigger the new task with evidence in the change note.
* Frontend changes: `npm run build` (in `web/`) + screenshot of the affected page.
* Infra changes: `az deployment sub what-if` (or `azd provision --preview`) output attached to the change note. For the bundled Container App, also confirm `postprovision.sh` still applies the six-sidecar template diff cleanly.
* **Do not** rely on `func start` for new work — the Azure Functions tree has been removed from the repository.

### Do NOT redeploy for ordinary code changes (NON-NEGOTIABLE)
Validation = pytest + local smoke (`uv run uvicorn …`, `npm run dev`, or the `fullstack: start` VS Code task — see [scripts/dev/README.md](../scripts/dev/README.md) "three-tier debug loop"). Do **not** run `scripts/dev/quick-deploy.sh`, `scripts/dev/postprovision.sh`, `az acr build`, or `azd provision` unless **both** of the following hold:

1. The change touches sidecar layout, Container App template, terminal toolchain (`terminal/Dockerfile*`, `exec_server.py`), or Bicep under `infra/`.
2. The bug or behaviour genuinely cannot be reproduced in Tier 1 (pytest) or Tier 2a (host-mode `fullstack: start`).

When you do redeploy, state the reason in the change note (which sidecar, which Tier 2a check was tried and why it failed). Building images "just to be sure" wastes 5-10 minutes per cycle and is a charter violation.

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

## 15. Where Things Live

Quick "where to edit" table moved to [docs/copilot/repo-layout.md](../docs/copilot/repo-layout.md). For the route map, tripwires, and validation cheatsheet see [AGENTS.md](../AGENTS.md).

