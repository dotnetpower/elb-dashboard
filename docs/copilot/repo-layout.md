---
title: Repository Layout (Agent Detail)
description: Full directory tree for elb-dashboard plus a where-to-edit map for AI coding agents and human contributors. Covers api/, web/, infra/, terminal/, and scripts/.
---

# Repository Layout (detail)

> Extracted from `.github/copilot-instructions.md` §4 on 2026-05-19 to keep the
> always-loaded charter lean. Read this on demand when you need the full tree.

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
│   ├── main.bicep               # Container Apps Environment + ca-elb-dashboard + private networking
│   └── modules/                 # containerApp.bicep, network.bicep, identity.bicep, acr.bicep, storage.bicep, keyVault.bicep, …
├── scripts/
│   └── dev/                     # Local dev helpers + postprovision.sh (runs `az acr build` and swaps the Container App template)
├── docs/
│   ├── auth.md
│   ├── container-apps-migration.md  # Authoritative target architecture
│   ├── copilot/                 # On-demand detail for Copilot instructions (this folder)
│   └── features_change/         # Per-change notes
├── tests/                       # Cross-cutting tests; per-component tests live next to their code
├── azure.yaml                   # azd manifest (Bicep provider + pre/postprovision hooks)
└── README.md
```

## Quick reference — Where things live

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

For deeper navigation (route map, tripwires) see [AGENTS.md](../../AGENTS.md).

---

## Backend module map (`api/`)

```
api/
├── main.py              # FastAPI app factory + middleware (RequestId, structured logs)
├── celery_app.py        # Celery app + queue routing (azure / blast / storage / default)
├── auth.py              # MSAL bearer-token validation (OIDC discovery + JWKS cache)
├── conftest.py          # pytest sys.path bootstrap
├── routes/              # FastAPI routers (one file per /api/<area>)
├── services/            # The ONLY place that touches azure.mgmt.* / azure.identity / k8s
│   ├── azure_clients.py # Cached Azure SDK client factories (MI under DefaultAzureCredential)
│   ├── monitoring.py    # AKS / Storage / ACR readers + k8s_* kubelet helpers
│   ├── network.py       # ensure_resource_group + VNet/Subnet/NSG primitives
│   ├── compute.py       # VM lifecycle helpers (only used by legacy code paths now)
│   ├── keyvault.py      # KV secret read/write
│   ├── storage_data.py  # Blob upload/list/read — NO SAS issuance, see charter §9 footgun note
│   ├── state_repo.py    # JobStateRepository (Table Storage + append-blob audit)
│   ├── sanitise.py      # Output redactor (SAS, bearer, sub-id, secrets) — apply at every UI boundary
│   ├── passwords.py     # generate_admin_password (used only by legacy, kept for tests)
│   ├── ssh_exec.py      # paramiko helpers (used only by legacy)
│   ├── blast_config.py  # Azure SKU / pricing constants
│   └── image_tags.py    # IMAGE_TAGS dict — bump in sync with sibling repo
├── tasks/               # Celery tasks live here (mostly empty; populate as you implement stub routes)
└── tests/               # `uv run pytest -q api/tests`
```

## Frontend module map (`web/src/`)

```
web/src/
├── App.tsx, main.tsx    # Router + MSAL provider wiring
├── api/
│   ├── client.ts        # fetch wrapper that injects MSAL bearer
│   ├── endpoints.ts     # Typed endpoint helpers (one source of /api/* surface)
│   ├── arm.ts           # Direct ARM token flow (used only where SPA needs subscription list)
│   ├── callerIp.ts      # Browser → ipify (caller IP capture)
│   └── resilience.ts    # Retry / backoff helpers + tests
├── auth/                # MSAL configuration
├── components/          # Reusable glass cards, modals, etc.
├── pages/               # Dashboard, BlastJobs, BlastResults, BlastAnalytics, RemoteTerminal, ...
├── hooks/, data/, theme/, constants.ts
```

UI tokens (glassmorphism) are CSS variables; see [docs/copilot/glass-ui.md](./glass-ui.md).

## Infra map (`infra/`)

[`infra/main.bicep`](../../infra/main.bicep) wires nine modules in this order:
`network → monitoring → identity → acr → storage → storageState → keyvault →
containerEnv → controlApp`. Each one is a single small file under
[`infra/modules/`](../../infra/modules/).

Public ingress lands on the `api` sidecar at `:8080`. All other sidecars
listen on loopback only:
- frontend nginx → `127.0.0.1:8081`
- terminal ttyd → `127.0.0.1:7681`
- redis → `127.0.0.1:6379`
