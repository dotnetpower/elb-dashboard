# elastic-blast-azure-functionapp — Copilot Instructions

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

1. **Web UI** (glassmorphic, calm/muted theme) hosts every action.
2. **Remote Terminal** (a VM provisioned on demand) is the *only* place the `elastic-blast` CLI runs — but it is treated as a tool, not as the primary surface.
3. The UI continuously **monitors** the AKS cluster, Storage Account / databases, ACR images, and ElasticBLAST job state, with **Durable Functions** tracking long-running work.
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

| Layer            | Choice                                                                       | Reason                                                                |
| ---------------- | ---------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| Backend runtime  | **Azure Functions, Python v2 programming model, Python 3.11**                | Matches `elastic-blast-azure` (Python 3.11). Repo name says functionapp. |
| Long-running     | **Durable Functions** (orchestrator + activity + entity)                     | VM provisioning, ACR builds, job-status polling all exceed HTTP limits. |
| Frontend         | **React + Vite + TypeScript**, deployed as Functions static assets or SWA    | Single bundle, glassmorphism is straightforward in CSS.               |
| Browser auth     | **MSAL.js (`@azure/msal-browser`) → Auth Code + PKCE**                       | Mirrors `az login` UX; backend validates the bearer token.            |
| Backend auth     | **`azure-identity` `OnBehalfOfCredential`** to call ARM as the signed-in user | Honors the user's RBAC; no shared secrets.                            |
| Browser shell    | **xterm.js + WebSocket proxy → SSH** (or Azure Bastion tunneling)            | Lets the user run `az login` and `elastic-blast` from the browser.    |
| IaC              | **Bicep** (`infra/`)                                                         | Idiomatic for Azure Functions / SWA / VM / Vault.                     |
| Deploy tooling   | **Azure Developer CLI (`azd`)**                                              | Single `azd up` to stand the platform up.                             |
| Secrets          | **Azure Key Vault** (VM password, SSH host key, etc.)                        | Never store secrets in env vars committed to the repo.                |

Pin Azure CLI ≥ 2.81, kubectl ≥ 1.34, azcopy ≥ 10.28, BLAST+ 2.17.0 — same versions validated by `elastic-blast-azure` on 2026-04-29.

---

## 4. Proposed Repository Layout

Create directories on demand; do not scaffold empty folders speculatively.

```
.
├── api/                         # Azure Functions (Python v2 model)
│   ├── function_app.py          # Entry point: HTTP triggers + DF starters
│   ├── orchestrators/           # Durable orchestrator functions
│   │   ├── provision_terminal.py
│   │   ├── build_acr_images.py
│   │   ├── monitor_aks.py
│   │   └── monitor_jobs.py
│   ├── activities/              # Activity functions (single-purpose)
│   ├── entities/                # Durable entities for state (e.g. TerminalState)
│   ├── services/                # Pure-Python wrappers around Azure SDK
│   ├── auth/                    # Token validation + OBO helpers
│   ├── models/                  # Pydantic models for request/response
│   ├── host.json
│   └── requirements.txt
├── web/                         # React + Vite + TypeScript SPA
│   ├── src/
│   │   ├── components/          # Glassmorphic UI building blocks
│   │   ├── pages/               # Dashboard, RemoteTerminal, JobDetail, …
│   │   ├── hooks/
│   │   ├── api/                 # Typed fetchers for /api routes
│   │   └── theme/               # Glassmorphism tokens (CSS variables)
│   └── vite.config.ts
├── infra/                       # Bicep modules + main.bicep + azure.yaml
│   ├── main.bicep
│   ├── modules/
│   │   ├── functionApp.bicep
│   │   ├── staticWebApp.bicep   # or skip if served from Functions
│   │   ├── keyVault.bicep
│   │   ├── storage.bicep
│   │   └── network.bicep
├── scripts/
│   ├── cloud-init/
│   │   └── remote-terminal.yaml # cloud-init applied to the VM
│   └── dev/                     # Local dev helpers
├── docs/
│   ├── architecture.md
│   ├── auth.md
│   ├── remote-terminal.md
│   └── features_change/         # Per-change notes (see §13)
├── tests/                       # pytest (api), vitest (web)
├── azure.yaml                   # azd manifest
└── README.md
```

---

## 5. Authentication Flow (must be interactive `az login`-equivalent)

1. SPA loads → MSAL acquires an **ID token + access token for the ARM resource** (`https://management.azure.com/.default`) via Auth Code + PKCE.
2. SPA calls `/api/*` with `Authorization: Bearer <access_token>`.
3. The Function App validates the JWT (issuer, audience, signing keys cached from the tenant's OpenID metadata) **before** any business logic runs. Reject all unauthenticated requests with 401.
4. For ARM calls, the backend uses `OnBehalfOfCredential` to exchange the user's token for a downstream ARM token — every Azure mutation runs with the user's identity, so RBAC failures surface to the user instead of silently succeeding under a privileged SP.
5. The Remote Terminal VM never holds a long-lived Azure credential. The user runs `az login --use-device-code` *inside the SSH session* the first time they connect. This is intentional and matches `azure-prereq.md` Step 2.

> Do **not** add a "service principal" / "client credentials" / "stored Azure password" code path. If you find yourself reaching for one, stop and ask.

---

## 6. Remote Terminal — Lifecycle

The Remote Terminal is a Linux VM (Ubuntu 22.04, recommend `Standard_D4s_v5`, OS disk ≥ 64 GB) that gives the user a working `elastic-blast` shell.

### 6.1 Defaults (all must be overridable in the UI)

| Field                | Default              |
| -------------------- | -------------------- |
| Resource group       | `rg-elb-terminal`    |
| Region               | `koreacentral`       |
| VM name              | `vm-elb-terminal`    |
| VM size              | `Standard_D4s_v5`    |
| Admin username       | `azureuser`          |
| Admin password       | **randomly generated**, 24 chars, mixed classes, stored in Key Vault, displayed **once** in the UI with a copy button |
| SSH port             | 22 (locked to the user's egress IP via NSG; UI captures it from the request) |
| Public IP            | Static, DNS label `elb-term-<short-hash>.<region>.cloudapp.azure.com` |

### 6.2 Provisioning orchestrator (`provision_terminal.py`)

Mirror these activity steps, each idempotent:

1. `ensure_resource_group(rg, region)`
2. `ensure_network(rg, region)` — VNet, subnet, NSG (allow 22 from caller IP only)
3. `generate_admin_password()` → Key Vault secret `vm-<name>-password`
4. `create_vm(rg, name, size, cloud_init=remote-terminal.yaml, admin_pw=…)`
5. `wait_for_cloud_init_complete(vm)` — poll `cloud-init status --wait` via Run Command
6. `publish_connection_info(vm)` → returns `{ssh_host, ssh_port, username, password_secret_uri, fqdn}`

Surface the orchestrator status to the UI via the standard DF status endpoint.

### 6.3 Cloud-init responsibilities (`scripts/cloud-init/remote-terminal.yaml`)

The VM must be ready for the user to start at **azure-prereq.md Step 2 (`az login`)** and proceed without installing anything. The cloud-init script must therefore complete *the parts of Steps 1, 5, and 6 that do not require Azure auth*:

* Install `azure-cli` ≥ 2.81, `kubectl` ≥ 1.34, `azcopy` ≥ 10.28, Python 3.11 + `python3.11-venv`, `git`, `make`, `jq`, `unzip`, `curl`, `tmux`.
* `git clone https://github.com/dotnetpower/elastic-blast-azure.git /home/azureuser/elastic-blast-azure`.
* `python3.11 -m venv /home/azureuser/elastic-blast-azure/venv` and `pip install -r requirements/test.txt`.
* Write a `~/.bashrc` snippet exporting `PYTHONPATH=src:$PYTHONPATH`, `AZCOPY_AUTO_LOGIN_TYPE=AZCLI`, `ELB_SKIP_DB_VERIFY=true`, `ELB_DISABLE_AUTO_SHUTDOWN=1`.
* Print a **MOTD** explaining the next step is `az login --use-device-code` and pointing at the configured RG / ACR / Storage Account names that the web app already created.
* Steps that *require* az login (Step 3 RG, Step 4 ACR, Step 6 image build, Step 7 Storage) are performed by the **backend** using OBO before/after VM provisioning — see §7. The VM only needs the runtime, not the infrastructure.

> Cloud-init must be **deterministic and re-runnable** (use `runcmd` guards). If a step fails the orchestrator retries the activity, not the whole VM.

### 6.4 Browser shell

* Page `RemoteTerminal` shows: connection card (host, user, password reveal, copy buttons), embedded `xterm.js` connected over WebSocket to a small SSH-proxy activity (or Azure Bastion's tunnel API). No download/install required.
* Display "Run `az login --use-device-code` first" as a one-time helper banner.

### 6.5 Teardown

Provide an explicit "Destroy Remote Terminal" action: deletes the VM, NIC, OS disk, public IP, and the password Key Vault secret. RG itself is *kept* (it may host other terminals in the future) unless the user ticks "Also delete resource group".

---

## 7. ElasticBLAST Resource Plane (driven by the backend, not the VM)

The web app is the source of truth for the *infrastructure* the elastic-blast CLI talks to. Implement these as Durable orchestrators so the UI can poll progress:

| Orchestrator             | Mirrors azure-prereq.md | Notes                                                                                                                       |
| ------------------------ | ----------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `ensure_resource_groups` | Step 3                  | Two RGs: `rg-elb` (workload) and `rg-elbacr` (registry). Both names + region configurable in the UI.                        |
| `ensure_acr`             | Step 4                  | Standard SKU. Idempotent. Output: login server.                                                                              |
| `build_acr_images`       | Step 6                  | Use **`az acr build` REST API** (no local Docker). Build `ncbi/elb:1.4.0`, `ncbi/elasticblast-job-submit:4.1.0`, `ncbi/elasticblast-query-split:0.1.4`. Report per-image status. |
| `ensure_storage`         | Step 7                  | HNS-enabled `Standard_LRS`. Containers `blast-db`, `queries`, `results`. Public network access toggled per §9.              |
| `monitor_aks`            | Step 9                  | Polls `aks list/show`, surfaces `provisioningState`, node count, `powerState`, kubelet identity, role assignments.          |
| `monitor_jobs`           | Step 9.3                | Polls `kubectl get jobs/pods` via the kubelet API or via SSH-Run-Command on the Remote Terminal. Persists history.         |

Image tags MUST stay in sync with `src/elastic_blast/constants.py` in the sibling repo. Regression check: any orchestrator that builds images reads tag values from a single `IMAGE_TAGS` dict that future contributors can update in one place. Hard-code today's pinned tags; re-validate when bumping.

---

## 8. Monitoring UI (primary surface)

The dashboard is the landing page; the Remote Terminal is one tab among many.

Required cards (each backed by a polled REST endpoint, 30 s default refresh):

1. **Cluster** — AKS name, RG, region, K8s version, node pool size/SKU, `powerState`, `provisioningState`, kubelet identity object id, attached ACR.
2. **Storage** — account name, region, public-access toggle (with "Enable for 5 min" affordance), container list, blob counts/sizes for `blast-db/`, `queries/`, `results/`.
3. **ACR** — registry, login server, repositories with tag table (highlight mismatches against `IMAGE_TAGS`).
4. **Jobs** — list of ElasticBLAST submissions with status (`Provisioning | Downloading DB | Splitting | Running | Completed | Failed | Deleted`), elapsed time, results URL. Drill-down opens the durable orchestrator's full event history.
5. **Remote Terminal** — VM state, FQDN, last `az login` heartbeat (parse from `~/.azure/azureProfile.json` mtime via Run Command), button to open the embedded shell.

All numbers must come from real Azure / Kubernetes APIs. Never fabricate or cache stale data without showing a "last refreshed" timestamp.

---

## 9. Storage Account public-network-access discipline

`elastic-blast` requires `publicNetworkAccess=Enabled` on the storage account during `submit/status/delete` (see `azure-prereq.md` §9). Encode this in the UI as a temporary toggle:

* Default state: **Disabled**.
* "Run ElasticBLAST" actions auto-enable for the duration of the orchestration, wait 15 s for propagation, then re-disable on completion or failure.
* Display the current state prominently and warn users when it is left enabled.

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

* Format with `ruff format`, lint with `ruff check`. No `black`/`isort` duplication.
* Type hints required on all public functions; `mypy --strict` clean.
* Pydantic v2 for request/response models; never accept untyped `dict` at HTTP boundaries.
* Azure SDK calls go through `services/` wrappers — orchestrators and activities must not import `azure.mgmt.*` directly.
* No `print` — use the standard `logging` module; structured logs (JSON) preferred.
* Activities are **idempotent** and **side-effect-tagged** in the docstring.

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
* [ ] All HTTP triggers validate the bearer token before doing work.
* [ ] All ARM mutations use OBO with the caller's token.
* [ ] NSG on the Remote Terminal restricts SSH to the caller's IP, not `0.0.0.0/0`.
* [ ] Generated VM password ≥ 20 chars, written only to Key Vault, returned to the UI exactly once over HTTPS.
* [ ] Storage account `publicNetworkAccess` is left `Disabled` after every operation completes (success or failure).
* [ ] Output of `az`/`kubectl` shown in the UI is sanitised — never echo tokens, subscription IDs, or full SAS URLs.
* [ ] Bicep deployments tagged with `azd-env-name` and `costCenter` for traceability.

---

## 13. Process Discipline

### Per-feature change notes
Before each commit that adds or alters user-visible behaviour, create:

```
docs/features_change/YYYY-MM/YYYY-MM-DD-<short-name>.md
```

containing: motivation, user-facing change, API/IaC diff summary, validation evidence (screenshot, curl, or test name).

### Validation before marking done
* Backend changes: `pytest -q` + `func start` smoke test of the new route/orchestrator.
* Frontend changes: `npm run build` + screenshot of the affected page.
* Infra changes: `az deployment sub what-if` (or `azd provision --preview`) output attached to the change note.

### Cross-repo consistency
When `dotnetpower/elastic-blast-azure` updates `src/elastic_blast/constants.py` image tags or the `azure-prereq.md` step structure, open a tracking issue here and bump `IMAGE_TAGS` / cloud-init in the same PR.

---

## 14. Out of Scope (explicit)

* Anything that requires the user to run a command on their own laptop.
* AWS / GCP code paths (the upstream supports them; this control plane is Azure-only).
* Multi-tenant SaaS hosting — assume one Azure tenant per deployment.
* Storing per-user state outside the user's own Azure subscription.

---

## 15. Quick Reference — Where Things Live

| Need to…                                  | Edit                                                |
| ----------------------------------------- | --------------------------------------------------- |
| Add a new monitoring card                 | `web/src/pages/Dashboard.tsx` + `api/function_app.py` route |
| Change the VM image / size defaults       | `api/orchestrators/provision_terminal.py` constants |
| Update tools installed on the VM          | `scripts/cloud-init/remote-terminal.yaml`           |
| Bump pinned ACR image tags                | `api/services/image_tags.py` (`IMAGE_TAGS` dict)    |
| Adjust glass styling                      | `web/src/theme/glass.css`                           |
| Add a new Bicep resource                  | `infra/modules/*.bicep` + wire into `main.bicep`    |
| Document a behaviour change               | `docs/features_change/YYYY-MM/…md` (mandatory)      |
