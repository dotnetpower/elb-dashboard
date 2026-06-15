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

> **Design-critique the plan (step 3) and the result (before done) for non-trivial backend/infra work** using the [self-critique-review skill](../.github/skills/self-critique-review/SKILL.md) rubric — contract/state-machine consistency, unbounded retry/wait loops, idempotency, concurrency races, partial-failure, observability. The mechanical self-review in §13 (consumer grep + tests + diff) cannot see these design-level defects, which are where most Critical/High critique findings come from. Designing them out up front is cheaper than patching them after a critique.

> **Issue discipline (NON-NEGOTIABLE).** If the work you do maps to a registered GitHub issue, you OWN that issue for the session:
> 1. **Comment** on the issue with what you shipped (commit SHA), the validation evidence, and — for partial work — the explicit remaining gaps. Never leave a worked issue silent.
> 2. **Close it** (`gh issue close <N>`) only when **every** acceptance criterion is met; otherwise leave it open with the gap spelled out. Partial work that satisfies only some criteria never closes the issue.
> 3. **Re-verify before closing**: run `gh issue view <N>`, read the acceptance criteria, and confirm each one against the implemented diff + validation. Do not close on optimism.
>
> This applies whether or not a commit message names the issue. The full mechanics (commit-reference trigger, wording) are in §13 "GitHub issue closure hygiene".

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
| Hosting          | **Azure Container Apps** — single Container App `ca-elb-dashboard` with six sidecars (`frontend`, `api`, `worker`, `beat`, `redis`, `terminal`); pinned `minReplicas: 1`, `maxReplicas: 1` | One billable revision, private VNet, queue-backed workers, sidecar terminal. No Function App, no Static Web App, no Remote Terminal VM. |
| API              | **FastAPI on Python 3.12** (`api/`), served by uvicorn in the `api` sidecar on `:8080` | Long-running operations, WebSocket terminal proxy, streaming upload/download proxy that don't fit the Functions HTTP model. |
| Long-running     | **Celery 5 worker + beat sidecars**, broker = in-revision `redis:7-alpine` sidecar (ephemeral; queue rebuilt from the `jobstate` table by the beat reconciler on revision restart) | Replaces Durable Functions. BLAST submit/delete, ACR builds, AKS provisioning, DB warmup, scheduled work. |
| Durable state    | **Azure Storage** — Table for job/audit/schedule rows, append blobs for command history; **no managed database** | Cost-minimised; documents/append workloads only. |
| Frontend         | **React + Vite + TypeScript** (`web/`), built into `dist/` and served by the `frontend` (nginx:alpine) sidecar at `127.0.0.1:8081`; the `api` sidecar reverse-proxies non-`/api/*` requests to it | Same-origin, no SWA resource. |
| Browser auth     | **MSAL.js (`@azure/msal-browser`) → Auth Code + PKCE**                       | Mirrors `az login` UX; backend validates the bearer token.            |
| Backend auth     | **`azure-identity` `DefaultAzureCredential`** using the user-assigned MI `id-elb-dashboard-*` (shared by all six sidecars) | All Azure SDK calls use MI; bearer token is for identity verification only. |
| Browser terminal | **xterm.js + WebSocket → loopback `ttyd` in the `terminal` sidecar** (no SSH, no VM, no admin password) | The `terminal` sidecar carries the `elastic-blast` toolchain; `/home/azureuser` is ephemeral and user files stage to workload Storage via `azcopy` (no Azure Files mount). |
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

This is intentionally an explicit local-debug action, not a production dashboard control — the friction is the safety mechanism. RBAC (`Storage Blob Data Reader` / `Contributor`) is unchanged and still enforced; the script only opens the network surface to the caller's public IP. The local backend may also call `api.services.storage.public_access.ensure_local_storage_access()` when `LOCAL_DEBUG_AUTO_OPEN_STORAGE=true` and a route has full Storage ARM scope; that helper must keep the `CONTAINER_APP_NAME` guard so deployed Container Apps can never flip Storage open. Do not check in any wrapper that calls this outside local debugging, and do not leave the surface open after debugging.

**One-shot local-debug session toggle.** When a developer wants the local dashboard caller to be their real `az login` identity (not the synthetic `anonymous` dev-bypass) — required to exercise data-plane routes such as `/api/blast/databases` — use the bundled session toggle instead of running each piece by hand:

```
scripts/dev/local-run.sh auth-on      # ensure RBAC + open storage + AUTH_DEV_BYPASS=false + restart api/web
# ... debug as real identity ...
scripts/dev/local-run.sh auth-off     # bypass=true + close storage + restart api/web (RBAC kept)
scripts/dev/local-run.sh auth-status  # show current state, no mutations
```

[scripts/dev/local-debug-auth.sh](../scripts/dev/local-debug-auth.sh) (also exposed as the `auth-on / auth-off / auth-status` subcommands above) is the canonical entry point: it composes `grant-local-rbac.sh` + `storage-public-access.sh` + env upsert + service restart in one idempotent flow, pre-checks that the caller can list role assignments at the storage scope (= can also create them), auto-detects `STORAGE_ACCOUNT_NAME` / `AZURE_RESOURCE_GROUP` / `ACR_NAME` / `API_CLIENT_ID` from `azd env get-values`, and on `off` reverts the bypass plus closes the network surface (RBAC is intentionally kept — cheap and removing it would re-trigger the 1-5 min propagation wait next session). It is a local-debug helper only: it must never gain a production code path, a dashboard control, or a Container App caller, and must keep the same `CONTAINER_APP_NAME` guards as the storage toggle it wraps.

Consequences for the code:

* All browser uploads/downloads of queries/results are **streamed through the `api` sidecar** (1 MiB chunks for download, 4 MiB block uploads, semaphore-capped to 4 concurrent transfers). This stays true regardless of the local-debug toggle state.
* **Never** issue SAS tokens to the browser. Never return a Storage URL the browser is expected to fetch directly.
* `elastic-blast` itself runs inside the `terminal` sidecar (same VNet) and reaches Storage via the same private endpoint, so the historical `publicNetworkAccess=Enabled` requirement during `submit/status/delete` does **not** apply here.
* The dashboard's Storage card surfaces the `publicNetworkAccess` value. **Enabled with `defaultAction=Deny` + a non-empty `ipRules` is an acceptable local-debug state**, but `Enabled` with `defaultAction=Allow`, or `Enabled` left over after debugging in a deployed environment, is an incident — remediate by running `storage-public-access.sh off` (or `scripts/dev/local-run.sh auth-off`, which closes the network *and* re-enables the bypass).

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
* **Git workflow — commit to the current branch, never branch or open a PR.**
  Work and commit on whatever branch is already checked out (normally `main`).
  Do **not** create feature branches, do **not** `git checkout -b`, and do
  **not** open pull requests (no `gh pr create`). Do **not** `git push` —
  pushing is the maintainer's call. Leave finished work as local commits on the
  current branch; the maintainer reviews `git log`/`git diff` and pushes when
  ready. (PR-oriented language elsewhere in this charter — "PR description",
  "apply to every PR" — describes review criteria for the eventual maintainer
  push, not an instruction for the agent to open a PR.)

---

## 12. Security Checklist (apply to every PR)

* [ ] No secrets in source, `.env`, or fixture files. Use Key Vault references.
* [ ] Every FastAPI route validates the MSAL bearer token before doing work (and the WebSocket handshake for `/api/terminal/ws` does the same — reject the upgrade if the token is missing/invalid).
* [ ] ARM and data-plane calls use the shared user-assigned MI `id-elb-dashboard-*` via `DefaultAzureCredential`. Do not introduce client secrets or OBO flows.
* [ ] **Every Storage account remains `publicNetworkAccess: Disabled`.** No code path enables it, even temporarily. Browser uploads/downloads stream through the `api` sidecar; no SAS tokens are issued to the browser.
* [ ] `ttyd` in the `terminal` sidecar binds to **127.0.0.1 only**. The Container App's public ingress targets the `api` sidecar on `:8080`; the terminal must never be reachable directly from the internet.
* [ ] Output of `az`/`kubectl`/Celery results shown in the UI is sanitised — never echo tokens, subscription IDs, or full SAS URLs.
* [ ] Bicep deployments carry the standard tag set (`azd-env-name`, `app`, `environment`, `costCenter`, `managedBy`, `repo`, `topology`, optional `owner`) computed in [infra/main.bicep](../infra/main.bicep) `var tags`, plus a per-module `role:` tag (`registry`, `control-plane`, `secrets`, …) merged in via `union(tags, { role: '…' })` inside each [infra/modules/*.bicep](../infra/modules/) — when adding a new module, follow the same pattern.

---

## 12a. Security Hardening Discipline (HARD REQUIREMENT)

This section is the safety net for §12. Any change that tightens auth, RBAC, network ACLs,
JWT validation, ticket lifetimes, CORS, or error sanitisation **must** also satisfy the rules
below. The goal is to make hardening additive and reversible so a single `quick-deploy.sh all`
can never silently strip permissions a subscription Owner / Contributor / Reader was relying on.

Scope (mandatory whenever a PR touches any of these surfaces):

* `api/auth.py`, `api/services/upgrade/auth.py`, anything that calls `require_caller`
* `infra/modules/*Roles*.bicep`, [infra/modules/storage.bicep](../infra/modules/storage.bicep),
  [infra/modules/acr.bicep](../infra/modules/acr.bicep),
  [infra/modules/keyvault.bicep](../infra/modules/keyvault.bicep),
  [infra/modules/workloadRgCreatorRole.bicep](../infra/modules/workloadRgCreatorRole.bicep)
* WebSocket / SSE ticket issuance and consumption ([api/routes/terminal/ws.py](../api/routes/terminal/ws.py),
  [api/routes/monitor/sidecars.py](../api/routes/monitor/sidecars.py),
  [api/routes/monitor/logs.py](../api/routes/monitor/logs.py))
* CORS / origin allowlists ([api/main.py](../api/main.py),
  [infra/modules/containerAppControl.bicep](../infra/modules/containerAppControl.bicep))
* [terminal/exec_server.py](../terminal/exec_server.py) (allowlist, bind host, body/timeout caps)

### Rule 1 — RBAC changes are always 2-phase (ADD then REMOVE)
Narrowing a role (e.g. `Contributor` → `Storage Blob Data Reader` + `AcrPull`) must not happen
in a single PR.

* **PR-N (phase-1)**: ADD the new narrower role assignments. Keep the existing broader role.
  The PR description must contain the literal phrase `phase-1 of 2 (see PR-…)` so reviewers can
  search for the matching phase-2.
* **Soak window**: At least one full release cycle (or 7 days of dogfood traffic, whichever is
  longer) with the new role active and the old role still present. The release notes for the
  phase-1 release must list the soak target.
* **PR-N+1 (phase-2)**: REMOVE the broader role. The PR description must reference PR-N's link
  and include the App Insights query showing zero authorization failures attributable to the
  narrower role during the soak window.

Single-PR role narrowing is rejected at review even if "obviously safe". The 2-phase rule has
no shortcuts and no exceptions for hotfixes — a hotfix that genuinely needs the broader role
must re-add it as phase-1.

### Rule 2 — Persona Matrix regression test is required
Any PR in scope above must keep [api/tests/test_persona_matrix.py](../api/tests/test_persona_matrix.py)
green. The matrix exercises four caller personas against a curated whitelist of actions:

| Persona | Expected to keep working |
|---------|--------------------------|
| `owner_caller` (subscription Owner) | full CRUD on platform + workload resources |
| `contributor_caller` (RG Contributor + Blob Data Contributor) | submit BLAST, AKS scale, ACR build, all data-plane writes |
| `reader_caller` (subscription Reader + Blob Data Reader) | dashboard browse, job list/status, logs, terminal open, AKS observe |
| `dev_bypass_caller` (`AUTH_DEV_BYPASS=true`, OID `00000…0`) | local-debug only — every action allowed but `is_dev_bypass_caller()` must remain True |

The Reader whitelist lives in `api/tests/persona_reader_allowlist.py`. Adding or removing an
entry in that file is a separate PR with a maintainer review; a hardening PR that needs the
Reader to lose an action must split into (a) "remove from whitelist" PR, then (b) the enforcement PR.

### Rule 3 — Postprovision Capability Probe must pass
[scripts/dev/postprovision.sh](../scripts/dev/postprovision.sh) runs
`scripts/dev/probe_capabilities.py` as its final step. The probe uses the deployed shared
user-assigned MI to attempt one real call against each critical Azure surface
(`BlobServiceClient.list_containers`, `TableServiceClient.list_tables`,
`ContainerRegistryManagementClient.registries.get`, `ManagedClustersOperations.get` when AKS
exists, `KeyClient.list_properties_of_keys`, `ContainerAppsAPIClient.container_apps.get`).

A 403 / `AuthorizationFailed` on any required surface aborts `postprovision.sh` with a non-zero
exit code, prints the missing role name, and links to the Bicep module that grants it.
`quick-deploy.sh all` invokes `postprovision.sh` and must not skip the probe. A new role
introduced in phase-1 of Rule 1 must be added to the probe in the same PR.

### Rule 4 — New guards ship default-OFF
Any new positive validation (e.g. `azp`/`appid` enforcement, JWT cache TTL shortening, CORS
allowlist narrowing, ticket IP-binding, exec_server bind-host pinning) is gated behind an
environment variable named `STRICT_<area>` or `ENFORCE_<area>`. Default = unset = **existing
behaviour preserved**. The PR that introduces the gate must:

1. Default the env var to OFF in [infra/modules/containerAppControl.bicep](../infra/modules/containerAppControl.bicep).
2. Add a positive and a negative test in `api/tests/` (the gate ON path AND the legacy OFF path).
3. Document the gate in [docs/operate/](../docs/operate/) with the planned flip date.

Flipping the default to ON is a separate PR that may only land after one full release cycle of
dogfood plus a green Persona Matrix run with the gate forced ON.

### Rule 5 — EventSource SSE never gets `require_caller`
The browser `EventSource` API cannot send `Authorization` headers, so the existing ticket-based
auth on [api/routes/monitor/sidecars.py](../api/routes/monitor/sidecars.py#L119) and
[api/routes/monitor/logs.py](../api/routes/monitor/logs.py#L79) must remain ticket-based. Adding
`Depends(require_caller)` to those event streams will break every dashboard log/metric tile and
is rejected at review.

The sanctioned hardening for SSE is to strengthen the ticket itself:

* Issue endpoint stays `require_caller`-protected (already is).
* Ticket payload binds to `caller.object_id`, `request.client.host` (or the trusted XFF first hop),
  and a `User-Agent` hash. Consume rejects if any of the three differ.
* Ticket is one-shot: first successful `_consume_*_ticket` invalidates it, even if TTL remains.
* Ticket TTL stays ≤ 30 s and the issue endpoint enforces an origin check identical to the
  WebSocket handler.

### Rule 6 — Hardening PR template (paste in description)
Every PR in scope must include the following checklist filled in:

```
Hardening discipline (§12a):
- [ ] In scope: <auth | rbac | network | jwt | ticket | cors | sanitise>
- [ ] RBAC change is single-PR safe (no role narrowed) OR labelled `phase-1 of 2` / `phase-2 of 2 (see #…)`
- [ ] Persona Matrix tests pass for owner / contributor / reader / dev_bypass
- [ ] Reader allowlist unchanged OR split-PR link: #…
- [ ] Capability Probe passes locally (`scripts/dev/probe_capabilities.py`)
- [ ] RBAC removal preflight green locally (`scripts/dev/preflight_rbac_removal.sh`) OR `ACCEPT_RBAC_REMOVAL=phase-2-of-pr-<N>` recorded in the PR description
- [ ] New guard ships default-OFF behind `STRICT_*` / `ENFORCE_*` env var (or N/A)
- [ ] No `Depends(require_caller)` added to an SSE event stream
- [ ] Change note under `docs/features_change/YYYY-MM/` summarises persona impact
```

Reviewers MUST refuse to merge a hardening PR with this block missing or incomplete.

### Rule 7 — RBAC removal halt at `azd provision`
Bicep is declarative, so removing a `roleAssignments` resource from a module
(or removing the module from `infra/main.bicep`) causes the next `azd provision`
/ `azd up` to **DELETE** the live assignment. That is exactly how Rule 1's
phase-2 PR is supposed to ship — but the same one-character diff in any unrelated
hardening PR will silently strip permissions a subscription Owner / Contributor /
Reader was relying on, and the first symptom is a `403 AuthorizationFailed` in
production traffic.

The mechanical guard for this is
[scripts/dev/check_rbac_removal.py](../scripts/dev/check_rbac_removal.py) and its
azure.yaml preprovision wrapper
[scripts/dev/preflight_rbac_removal.sh](../scripts/dev/preflight_rbac_removal.sh).
The wrapper runs `az deployment sub what-if --no-pretty-print --output json`
against `infra/main.bicep`, pipes the result to the python parser, and the
parser flags every `Microsoft.Authorization/roleAssignments` entry with
`changeType` in `Delete` / `DeploymentMode`.

Defaults (per Rule 4):

* `STRICT_RBAC_REMOVAL_HALT` unset / `false` → **warn-only**. Findings print to
  preprovision stdout; `azd provision` proceeds. This is the soak-window state.
* `STRICT_RBAC_REMOVAL_HALT=true` → **halt**. The preprovision hook exits
  non-zero, azd aborts before any ARM call. The only way through is to set
  `ACCEPT_RBAC_REMOVAL=phase-2-of-pr-<N>` (regex tolerates
  `phase-2 of 2 (see PR-<N>)` etc.) for the duration of the run.

When to flip the default to ON: after one full release cycle of dogfood with
the warning visible in preprovision logs AND a green `pytest -q
api/tests/test_check_rbac_removal.py` run with the gate forced ON. The flip is
its own PR; it must include the matching env var change in
[infra/modules/containerAppControl.bicep](../infra/modules/containerAppControl.bicep)
documentation only — there is no Container App surface to flip; the gate lives
in azd preprovision.

Phase-2 PRs MUST cite the matching phase-1 PR number in their
`ACCEPT_RBAC_REMOVAL` value and copy the same string into the Rule 6 checklist
("RBAC removal preflight green locally … OR `ACCEPT_RBAC_REMOVAL=phase-2-of-pr-<N>`
recorded"). Reviewers cross-check the value against the phase-1 PR before merge.

Scope notes:

* The guard runs `az deployment sub what-if` against
  [infra/main.bicep](../infra/main.bicep) only. That template is the single
  subscription-scope entry point and `az` flattens every nested module's
  changes into a single `changes[]` array, so module-level
  `roleAssignments` deletions ARE detected — no extra per-module scan
  needed. If a future PR introduces a *second* `targetScope = 'subscription'`
  entry point that azd provisions, add it to the wrapper's `--template-file`
  loop in the same PR.
* In `STRICT_RBAC_REMOVAL_HALT=true` mode the wrapper no longer silently
  skips on internal failures (missing `az`, unset `AZURE_*`, what-if call
  failed, malformed JSON). A skipped preflight in strict mode is treated
  as exit 3 — better a noisy halt than a silent permission loss. In
  warn-only mode the legacy silent-skip is preserved so day-to-day
  development is never blocked by environmental hiccups.
* Local validation: `bash scripts/dev/preflight_rbac_removal.sh` runs the
  same flow azd's preprovision invokes. Set
  `STRICT_RBAC_REMOVAL_HALT=true` to rehearse the halt path against your
  current env before opening a PR; the wrapper prints a per-finding line,
  an audit-friendly `ACCEPT_RBAC_REMOVAL=…` echo when the override is
  accepted, and a final `SUMMARY:` line so the outcome is visible at the
  bottom of a long log.

---

## 13. Process Discipline

### Documentation terminology links
When adding or updating documentation under `docs/`, link important external technical terms on first meaningful use in the document. Use this for platform/framework/product concepts a researcher or maintainer may want to look up, such as Azure Container Apps, AKS, Azure Storage, MSAL, managed identity, Redis, Celery, WebSocket, and ttyd.

Do not link every repeated occurrence. Prefer one first-use link per document section or page so the prose stays readable. Use authoritative sources: Microsoft Learn for Azure services and identity, official project documentation for open-source tools, and MDN for browser platform APIs. Keep internal documentation links same-tab; external documentation links are handled globally by `docs/javascripts/external-links.js` and should open in a new tab with `rel="noopener noreferrer"`.

### Per-feature change notes
Before each commit that adds or alters user-visible behaviour, create:

```
docs/features_change/YYYY-MM/YYYY-MM-DD-<short-name>.md
```

containing: motivation, user-facing change, API/IaC diff summary, validation evidence (screenshot, curl, or test name).

### GitHub issue closure hygiene
When work is tied to a registered GitHub issue, do not leave the issue silent. Before marking the task done, verify the issue's acceptance criteria against the implemented diff and validation evidence. If the criteria are met, add an issue comment summarising the shipped change and validation, then close the issue. If anything remains, leave the issue open and comment with the completed work, validation evidence, and explicit remaining gap.

**Commit-reference trigger (do not skip).** Whenever a commit message references an issue (`(#N)`, `fixes #N`, `closes #N`, `refs #N`), that issue MUST be updated in the same session — a referenced-but-silent issue is a process violation. Concretely: after committing work that names an issue, run `gh issue view <N>` to re-read its acceptance criteria, then either (a) comment + `gh issue close <N>` when every criterion is met, or (b) comment with the completed subset, validation evidence, and the explicit remaining gaps when only part of the issue shipped (keep it open). Partial work that closes only one of several acceptance criteria never closes the issue.

### Validation before marking done
* Backend changes (`api/`): `uv run pytest -q api/tests` + a local smoke test (`uv run uvicorn api.main:app --reload` for HTTP routes; `uv run celery -A api.celery_app worker -l info` for task changes). Curl the new route or trigger the new task with evidence in the change note.
* Frontend changes: `npm run build` (in `web/`) + screenshot of the affected page.
* Infra changes: `az deployment sub what-if` (or `azd provision --preview`) output attached to the change note. For the bundled Container App, also confirm `postprovision.sh` still applies the six-sidecar template diff cleanly.
* **Do not** rely on `func start` for new work — the Azure Functions tree has been removed from the repository.

### CI parity — mirror the GitHub Actions gates before every push (NON-NEGOTIABLE)
A push must never turn the Actions dashboard red. Two workflows gate `main` and PRs, and you must reproduce both **locally** before pushing:

| Workflow | File | What it runs | Local equivalent |
| --- | --- | --- | --- |
| Tests | [.github/workflows/test.yml](.github/workflows/test.yml) | `uv run ruff check api` + `uv run pytest -q api/tests` | same two commands |
| Publish Docs | [.github/workflows/docs.yml](.github/workflows/docs.yml) | `check_frontmatter.py` + `mkdocs build --strict` | `uv run python scripts/docs/check_frontmatter.py` then `DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict` |

The repo ships version-controlled git hooks that run exactly these checks automatically — **install them once per clone**:

```bash
scripts/dev/install-git-hooks.sh   # sets core.hooksPath=scripts/dev/git-hooks
```

* **pre-commit** (fast, staged files only): `ruff check api` when `api/**` is staged; the docs frontmatter guard when `docs/**` / `mkdocs.yml` is staged.
* **pre-push** (full CI mirror): `pytest -q api/tests` and/or `mkdocs build --strict`, scoped to the file paths the push actually touches (so a docs-only push skips pytest and vice-versa).

The hooks are the safety net, not a substitute for thinking: when you change `mkdocs.yml`-relevant docs, confirm every new page under `docs/**` is wired into the `nav:` (an orphan page fails `--strict`). Bypass only for genuine emergencies with `git commit/push --no-verify` (or `ELB_SKIP_HOOKS=1`), and never push a red build knowingly.


### Post-implementation self-review (NON-NEGOTIABLE for code changes)
After every code change and **before** calling `task_complete`, run a self-review pass without waiting for the user to ask. The goal is to catch broken contracts, missed consumers, and stale fixtures that the focused test you already ran would not surface.

Mandatory checklist (skip only the steps that obviously do not apply to the change):

1. **Consumer search** — for each modified function, route, response field, or TypeScript type, grep the workspace for every caller. Verify each one still works with the new contract. Pay special attention to:
   * other routes / services / tasks reading the same payload field;
   * frontend hooks, components, tests, and mocks that consume the same API;
   * tests that compare exact dict / object equality (these break silently on additive fields).
2. **Backward-compat check** — new fields default to optional / nullable; removed fields have a deprecation path; renamed symbols keep a re-export shim if any external caller might still use them.
3. **Wide test sweep** — run the full relevant suite, not just the focused file you wrote tests in. Backend: `uv run pytest -q api/tests`. Frontend: `cd web && npm test -- --run`. Infra: re-run the preview.
4. **Lint + build** — `uv run ruff check api/` on touched paths and `cd web && npm run build` whenever any `web/src/**` file changed.
5. **Diff audit** — `git status --short` + `git diff --stat <my files>` to confirm only the files you intended to touch are dirty, and the insertion/deletion counts match the plan. Investigate any unexpected file.
6. **Fixture / mock parity** — search `web/src/mocks/**`, `api/tests/**` fixtures, and any sample payloads in docs for the changed field shape and update them so they keep matching the live contract.

Report the self-review outcome in the final user-facing message (one short paragraph: what was checked, what passed, anything flagged). If anything is unresolved, do **not** call `task_complete` — fix it or escalate to the user with the specific blocker.

### Do NOT redeploy for ordinary code changes (NON-NEGOTIABLE)
Validation = pytest + local smoke (`uv run uvicorn …`, `npm run dev`, or the `fullstack: start` VS Code task — see [scripts/dev/README.md](../scripts/dev/README.md) "three-tier debug loop"). Do **not** run `scripts/dev/quick-deploy.sh`, `scripts/dev/postprovision.sh`, `az acr build`, or `azd provision` unless **both** of the following hold:

1. The change touches sidecar layout, Container App template, terminal toolchain (`terminal/Dockerfile*`, `exec_server.py`), or Bicep under `infra/`.
2. The bug or behaviour genuinely cannot be reproduced in Tier 1 (pytest) or Tier 2a (host-mode `fullstack: start`).

When you do redeploy, state the reason in the change note (which sidecar, which Tier 2a check was tried and why it failed). Building images "just to be sure" wastes 5-10 minutes per cycle and is a charter violation.

### Cross-repo consistency
When `dotnetpower/elastic-blast-azure` updates `src/elastic_blast/constants.py` image tags or the `azure-prereq.md` step structure, open a tracking issue here and bump `IMAGE_TAGS` / cloud-init in the same PR.

### Version stamp & release bump
The SPA header carries `v<A>.<B>.<build> · <short-sha>` next to "Control Plane". The release version comes from [web/package.json](../web/package.json) (single source of truth — [pyproject.toml](../pyproject.toml) is kept in sync) and is stored as `A.B.0`; the build number is computed at build time from commits since the latest `vA.B.0` tag; the short SHA comes from `git rev-parse --short HEAD`. These values are injected at build time via `vite.config.ts` `define`, and [scripts/dev/quick-deploy.sh](../scripts/dev/quick-deploy.sh) `frontend` plus [scripts/dev/postprovision.sh](../scripts/dev/postprovision.sh) resolve them on the host and pass them to `az acr build` as `--build-arg` (the ACR context has no `.git`).

Bump the release version with [scripts/dev/bump-version.sh](../scripts/dev/bump-version.sh):

* **A** — manual only (`--major`). Breaking product generation change you decide to ship.
* **B** — auto when any commit since the last `vA.B.0` tag starts with `feat:` / `fix:` (scoped forms included), or manual with `--release` / `--minor`.
* **C/build** — never committed. It is computed by frontend builds from the latest release tag to `HEAD`.

The script refuses to auto-bump if a `BREAKING CHANGE` footer or `feat!:` / `fix!:` marker is detected — pass `--major` to acknowledge. It rewrites `web/package.json` + `pyproject.toml`, creates `chore(release): vA.B.0` + annotated tag, and does **not** push. Maintainer pushes with `git push origin <branch> --follow-tags`. Do not edit `version` in either file by hand — go through the script so the release commit and tag stay consistent. Full workflow + troubleshooting: [docs/copilot/version-management.md](../docs/copilot/version-management.md).

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

