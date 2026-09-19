---
title: Repository Layout (Agent Detail)
description: Full directory tree for elb-dashboard plus a where-to-edit map for AI coding agents and human contributors. Covers api/, web/, infra/, terminal/, and scripts/.
tags:
  - agent
---

# Repository Layout (detail)

> Re-verified 2026-09-16 against the current workspace tree. Read this on
> demand when you need the full tree.

Create directories on demand; do not scaffold empty folders speculatively.

```
.
├── api/                     # Backend — FastAPI for the `api` sidecar + Celery worker/beat
│   ├── main.py                  # FastAPI app entrypoint (uvicorn target)
│   ├── celery_app.py            # Celery app + queue routing
│   ├── run_celery_workers.py    # Four isolated worker parents
│   ├── auth.py                  # MSAL bearer token validation
│   ├── _http_utils.py           # Shared HTTP boundary helpers
│   ├── routes/                  # FastAPI domain packages (monitor, blast, aks, settings, terminal, …)
│   ├── services/                # Azure/K8s/Storage/domain wrappers and focused packages
│   ├── tasks/                   # Celery task families (azure, blast, storage, servicebus, upgrade, …)
│   ├── tests/                   # pytest (FastAPI + Celery + shared service modules)
│   └── Dockerfile               # Image used by both `api` and `worker`/`beat` sidecars
├── web/                         # React + Vite + TypeScript SPA + Dockerfile + nginx.conf for the `frontend` sidecar
│   ├── src/
│   │   ├── components/          # Shared cards, controls, dialogs, and layout building blocks
│   │   ├── pages/               # Dashboard, BrowserTerminal, JobDetail, …
│   │   ├── hooks/
│   │   ├── api/                 # Typed fetchers for /api routes
│   │   └── theme/               # Dark/light UI tokens (CSS variables)
│   ├── nginx.conf               # nginx config for the `frontend` sidecar
│   └── vite.config.ts
├── terminal/                    # Dockerfile + entrypoint for the `terminal` sidecar (ttyd + elastic-blast toolchain)
├── infra/                       # Bicep modules + main.bicep
│   ├── main.bicep               # Container Apps Environment + ca-elb-dashboard + private networking
│   └── modules/                 # containerAppControl, network, identity, RBAC, storage, keyvault, …
├── scripts/
│   └── dev/                     # Local dev helpers + postprovision.sh (runs `az acr build` and swaps the Container App template)
├── docs/
│   ├── architecture/            # Authoritative architecture references (container-apps, runtime-plan, storage-contract, authentication)
│   ├── copilot/                 # On-demand detail for Copilot instructions (this folder)
│   ├── operate/                 # Operator-facing references (cli-upgrade, …)
│   ├── user-guide/              # End-user documentation
│   ├── research/                # Research notes that informed design decisions
│   └── features_change/         # Per-change notes
├── pyproject.toml               # uv-managed Python deps (runtime + dev) — single source of truth, no requirements.txt
├── uv.lock                      # Locked dependency versions (commit with pyproject.toml)
├── azure.yaml                   # azd manifest (Bicep provider + pre/postprovision hooks)
└── README.md
```

## Quick reference — Where things live

| Need to…                                  | Edit                                                |
| ----------------------------------------- | --------------------------------------------------- |
| Add a new monitoring card                 | `web/src/pages/Dashboard/DashboardGrid.tsx` + `web/src/components/cards/` + a route in `api/routes/monitor/<area>.py` |
| Add a new HTTP route                      | `api/routes/<area>.py` + register in `api/main.py` |
| Add a new long-running operation          | focused module under `api/tasks/<area>/` + an enqueue endpoint in `api/routes/` |
| Change terminal toolchain / runtime       | `terminal/Dockerfile.base` or `terminal/Dockerfile.runtime` + `terminal/entrypoint.sh` |
| Bump pinned ACR image tags                | `api/services/image_tags.py` (`IMAGE_TAGS` dict) |
| Adjust glass styling                      | `web/src/theme/glass.css`                           |
| Add a new Bicep resource                  | `infra/modules/*.bicep` + wire into `infra/main.bicep` |
| Change Container App sidecar layout       | `infra/modules/containerAppControl.bicep` + the template flow in `scripts/dev/postprovision.sh` |
| Document a behaviour change               | `docs/features_change/YYYY-MM/…md` (mandatory)      |

For deeper navigation (route map, tripwires) see [AGENTS.md](../../AGENTS.md).

---

## Backend module map (`api/`)

```
api/
├── main.py              # FastAPI app factory + middleware (RequestId, structured logs)
├── celery_app.py        # Celery routes/schedules (interactive + isolated maintenance queues)
├── run_celery_workers.py # main / reconcile / servicebus / artifact parents
├── auth.py              # MSAL bearer + optional shared-token M2M validation
├── routes/              # FastAPI routers (files and focused subpackages)
├── services/            # Azure SDK boundary plus domain/K8s/Storage packages
│   ├── azure_clients.py # Cached Azure SDK client factories (MI under DefaultAzureCredential)
│   ├── monitoring/      # AKS / Storage / ACR / provisioning compatibility façade
│   ├── k8s/             # Direct Kubernetes clients, metrics, jobs, Lease, runtime GC
│   ├── storage/         # Blob/DFS I/O, private networking, DB preparation, retention
│   ├── blast/           # Submit normalization, state projection, parsing, coordination
│   ├── network.py       # ensure_resource_group + VNet/Subnet/NSG primitives
│   ├── keyvault.py      # Key Vault provisioning/access helpers
│   ├── state_repo.py    # JobStateRepository (Table Storage + append-blob audit)
│   ├── sanitise.py      # Output redactor (SAS, bearer, sub-id, secrets) — apply at every UI boundary
│   ├── passwords.py     # generate_admin_password (used only by legacy, kept for tests)
│   └── image_tags.py    # IMAGE_TAGS dict — bump in sync with sibling repo
├── tasks/               # Implemented Azure, ACR, BLAST, Storage, OpenAPI, SB, upgrade tasks
└── tests/               # `uv run pytest -q api/tests`
```

## Frontend module map (`web/src/`)

```
web/src/
├── App.tsx, main.tsx    # Router + MSAL provider wiring
├── api/
│   ├── client.ts        # fetch wrapper that injects MSAL bearer
│   ├── endpoints.ts     # Typed endpoint compatibility façade
│   ├── generated/       # OpenAPI-generated declarations checked for drift
│   ├── arm.ts           # Direct ARM token flow for caller-visible subscription discovery
│   ├── armProxy.ts      # Same-origin dashboard ARM proxy client
│   └── resilience.ts    # Retry / backoff helpers + tests
├── auth/                # MSAL configuration
├── components/          # Reusable glass cards, modals, etc.
├── pages/               # Dashboard, BlastJobs, BlastResults, BlastAnalytics, RemoteTerminal, ...
├── hooks/, data/, theme/, constants.ts
```

UI tokens are CSS variables; see [Dashboard UI](./glass-ui.md).

## Infra map (`infra/`)

[`infra/main.bicep`](../../infra/main.bicep) wires the platform modules in this
order: network, monitoring, identity, subscription/control-plane/workload RBAC,
optional DNS-zone RBAC, ACR, Storage/state, Key Vault, Container Apps
Environment, and the bundled control app. Each module lives under
[`infra/modules/`](../../infra/modules/).

Public ingress lands on the `api` sidecar at `:8080`. All other sidecars
listen on loopback only:
- frontend nginx → `127.0.0.1:8081`
- terminal ttyd → `127.0.0.1:7681`
- terminal exec server → `127.0.0.1:7682`
- redis → `127.0.0.1:6379`
