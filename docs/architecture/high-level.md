---
title: High Level Architecture
description: Architectural map of ElasticBLAST Control Plane — browser and optional Service Bus ingress, the six-sidecar Azure Container App, private Storage, and AKS-backed execution.
social:
  cards_layout_options:
    title: High Level Architecture
    description: Browser and queue ingress, six sidecars, private Storage, and AKS-backed execution — how the pieces fit together.
tags:
  - architecture
---

# High Level Architecture

ElasticBLAST Control Plane separates the research workflow from the cloud operations needed to run it. Researchers stay in the browser; the control plane coordinates Azure identity, storage, images, AKS jobs, terminal access, and result delivery behind the scenes.

!!! tip "TL;DR"

    One Azure Container App (`ca-elb-dashboard`) hosts six sidecars
    (`frontend`, `api`, `worker`, `beat`, `redis`, `terminal`). The api
    sidecar fronts every browser request, talks to Azure as a user-assigned
    managed identity, and dispatches long-running BLAST work to Celery
    workers through the in-revision Redis broker. BLAST jobs themselves run
    on AKS. An optional, default-OFF Service Bus integration adds queue-based
    submit and completion-event paths without replacing Redis. Storage stays
    `publicNetworkAccess: Disabled`; no SAS tokens reach the browser.

Use this page as the first architecture map. It explains the major boundaries and flows, then links to the deeper implementation references.

## Architecture At A Glance

```mermaid
%%{init: {"theme": "base", "themeVariables": {"background": "#ffffff", "primaryColor": "#eef4f8", "primaryBorderColor": "#4a6b82", "primaryTextColor": "#172033", "lineColor": "#64748b", "secondaryColor": "#edf6f1", "tertiaryColor": "#f7f1eb", "clusterBkg": "#fbfcfd", "clusterBorder": "#b9c5d1", "fontFamily": "Inter, ui-sans-serif, system-ui, sans-serif"}, "flowchart": {"curve": "basis", "padding": 18, "htmlLabels": false}}}%%
flowchart TB
  researcher([Researcher]) --> browser["Browser SPA<br/>Dashboard / Search / Jobs / Results / Settings / Terminal"]
  entra["Microsoft Entra ID<br/>MSAL Auth Code + PKCE"] -. access token .-> browser
  external["External systems<br/>HTTPS API / queue producer / event consumer"]

  subgraph app["Azure Container App: ca-elb-dashboard"]
    direction TB
    ingress["Container Apps ingress<br/>TLS / one public origin / api :8080"]
    api["api sidecar<br/>FastAPI / auth / same-origin proxy / streaming"]
    frontend["frontend sidecar :8081<br/>nginx serves React dist"]
    worker["worker sidecar<br/>four isolated Celery worker pools"]
    beat["beat sidecar<br/>periodic scheduling / reconciliation"]
    redis[("redis sidecar :6379<br/>Celery broker + result backend + ops cache")]
    terminal["terminal sidecar<br/>ttyd :7681 + authenticated exec server :7682"]

    ingress --> api
    api -->|non-/api/* proxy| frontend
    api -->|interactive and long-running tasks| redis
    beat -->|scheduled tasks| redis
    redis -->|main / reconcile / servicebus / artifacts| worker
    api -->|WebSocket to ttyd| terminal
    api -->|allowlisted CLI RPC| terminal
    worker -->|allowlisted CLI RPC| terminal
  end

  subgraph messaging["Optional External Messaging - Default OFF"]
    serviceBus[("Azure Service Bus<br/>request queue + optional completion topic")]
  end

  subgraph azure["Azure Platform and Data Services"]
    direction TB
    access["Dashboard Azure access boundary<br/>id-elb-dashboard-* RBAC + VNet private endpoints"]
    platformStorage[("Platform Storage<br/>job state / audit / schedules / config / outbox")]
    workloadStorage[("Workload Storage<br/>databases / queries / results")]
    shared["Key Vault + Container Registry<br/>secrets / control-plane and workload images"]
    monitor["Application Insights + Log Analytics<br/>requests / logs / metrics / traces"]

    access --> platformStorage
    access --> workloadStorage
    access --> shared
  end

  subgraph workload["AKS Workload Plane"]
    direction TB
    openapi["OpenAPI execution service<br/>private by default / token protected<br/>id-elb-openapi federated identity"]
    jobs["Database prepare / warmup + ElasticBLAST jobs<br/>node cache / split / align / merge / finalize"]

    openapi --> jobs
  end

  browser -->|HTTPS + MSAL bearer token| ingress
  external -->|HTTPS + shared token| ingress

  api -->|direct submit / status| openapi
  api -. eligible submit .-> serviceBus
  external -. request enqueue .-> serviceBus
  serviceBus -. admitted request .-> worker
  worker -->|validated submit / status reconciliation| openapi
  worker -. durable transition outbox .-> serviceBus
  serviceBus -. event + claim-check link .-> external

  api -->|managed identity / state / streams| access
  worker -->|managed identity / checkpoints / outbox| access
  terminal -->|managed identity / azcopy| access
  terminal -->|kubectl / elastic-blast| jobs
  jobs -->|databases / queries / results| workloadStorage
  openapi -. federated identity .-> workloadStorage

  api --> monitor
  worker --> monitor
  jobs --> monitor

  classDef actor fill:#eff6ff,stroke:#4f6b88,color:#172033,stroke-width:1.4px;
  classDef app fill:#eef4f8,stroke:#4a6b82,color:#172033,stroke-width:1.3px;
  classDef broker fill:#fbf7ed,stroke:#9a7a32,color:#172033,stroke-width:1.3px;
  classDef state fill:#edf6f1,stroke:#4f7a67,color:#172033,stroke-width:1.3px;
  classDef workload fill:#f7f1eb,stroke:#9a6b48,color:#172033,stroke-width:1.3px;
  classDef security fill:#f4f5f7,stroke:#697586,color:#172033,stroke-width:1.2px;
  classDef optional fill:#fffaf0,stroke:#9a7a32,color:#172033,stroke-width:1.2px,stroke-dasharray:5 3;

  class researcher,browser,external actor;
  class frontend,api,worker,beat,terminal app;
  class redis broker;
  class serviceBus optional;
  class platformStorage,workloadStorage state;
  class openapi,jobs workload;
  class ingress,entra,access,shared,monitor security;
```

[Download the full-resolution PNG (7680 x 3482)](../images/high-level-architecture.png){ .md-button download="high-level-architecture.png" }

## Main Boundaries

The system has four practical boundaries.

| Boundary | What Lives There | Why It Matters |
|----------|------------------|----------------|
| Browser workflow | Dashboard, New Search, Jobs, Results, Analytics, API Reference, Settings (incl. self-upgrade), and the preview-gated Terminal / Database Builder / Tools pages | Researchers operate from one browser session instead of stitching together local commands. |
| Control plane | One Azure Container App with `frontend`, `api`, `worker`, `beat`, `redis`, and `terminal` sidecars | Operational work is coordinated in one low-cost, always-on revision; isolated worker queues keep interactive, reconciliation, Service Bus, and artifact work from starving one another. |
| External messaging (optional) | Service Bus request queue and optional completion topic, disabled by default | External producers gain durable ingress, back-pressure, and claim-check completion events without turning Service Bus into the Celery broker. |
| Workload plane | AKS, ElasticBLAST jobs, OpenAPI execution service, BLAST databases, queries, and results | Search execution remains isolated from the control plane that manages it. |

## Request Flow

The browser calls the `api` sidecar with an [MSAL][msal] bearer token. The API validates the token before doing work. For Azure operations, the backend uses the shared [user-assigned managed identity][managed-identity] rather than forwarding the user's browser token to Azure services.

Trusted automation may instead authenticate with the shared
`X-ELB-API-Token` while `ALLOW_OPENAPI_TOKEN_AUTH=true`. The shared deployment
default enables this M2M path for every `require_caller` route, so the token is
an administrator credential and ingress must remain controlled. Both browser
and M2M requests still perform downstream Azure work as the dashboard managed
identity.

Small read operations return directly from the API. Long-running work is queued through [Redis][redis] and executed by isolated [Celery][celery] worker pools for interactive, reconciliation, Service Bus, and artifact work. The UI polls durable task and job state from the API so researchers see progress instead of a blank spinner.

When the optional [Azure Service Bus][service-bus] integration is enabled, eligible unscoped submissions can enter its request queue and external systems can use the same queue directly. A dedicated Celery worker drains requests only after execution admission is ready, then submits them to the same OpenAPI service as the direct path. Optional completion events contain claim-check links to API-hosted results, not result payloads. Redis remains the internal Celery broker in every mode.

## BLAST Job Flow

1. The researcher submits a search from New Search or uses the API Reference / OpenAPI route.
2. The API validates and normalizes the request, then either submits directly to OpenAPI or, when enabled and eligible, writes it to the Service Bus request queue.
3. When the target database is not yet staged, a prepare-DB step runs as an [AKS][aks] job that downloads and verifies the BLAST database before the search starts.
4. The workload plane runs ElasticBLAST jobs on AKS, started on demand and stopped automatically when the cluster goes idle to keep compute cost low.
5. Job state, events, schedules, audit history, and the Service Bus response outbox are written to platform [Azure Storage][azure-storage].
6. Databases, queries, and result files stay in workload Storage. The API stages inline query input, serves bounded previews, and streams result-file downloads.
7. The browser opens the Jobs, Results, and Analytics pages to inspect completion, logs, files, and result analytics.

The AKS cluster has its own lifecycle. The dashboard starts it on demand and the `beat` reconciler evaluates idle time, so a cluster left running auto-stops once no live BLAST activity remains. The Jobs and cluster cards surface a live countdown to the next auto-stop.

## Storage And Network Model

The browser never receives Storage SAS tokens and never connects directly to private Storage endpoints. Inline query JSON is staged by the `api` sidecar, previews are returned as bounded API responses, and result-file downloads stream through the sidecar. Platform Storage holds control-plane state and the durable event outbox; workload Storage holds databases, queries, and results.

Every workload Storage account stays `publicNetworkAccess: Disabled` from day one. The [Container App][azure-container-apps] reaches Storage through [private endpoints][private-endpoints] inside the platform network. Developers iterating from a laptop use the explicit local-debug helper (`scripts/dev/storage-public-access.sh on|off`) which opens an IP-allowlisted window for the caller and refuses to run inside a deployed Container App. There is no deployed code path that flips public access on.

## Workload Connectivity

The `api` sidecar discovers and proxies the [OpenAPI][openapi] execution service that fronts ElasticBLAST on AKS. The direct API path and optional Service Bus drain converge on this same service. It is reached on its private IP, so the control-plane platform VNet is peered with the AKS cluster VNet. When the cluster runs in bring-your-own (BYO) subnet mode the AKS VNet is the dashboard platform VNet itself, so the api sidecar already reaches the private IP directly and peering is skipped as a no-op. The OpenAPI pod uses its own federated workload identity (`id-elb-openapi`), separate from the Container App's dashboard identity.

The OpenAPI surface is private by default. Operators who need an externally reachable HTTPS endpoint can opt into Public HTTPS, which provisions an ingress controller with a cert-manager Let's Encrypt certificate and reconciles the node-subnet NSG so the inbound 80/443 path works on BYO-subnet clusters. Calls still carry the `X-ELB-API-Token` admin header and are rate-limited by the api sidecar.

The browser terminal is not a VM. It is the `terminal` sidecar in the same Container App revision. The browser opens a [WebSocket][websocket] to the API sidecar, and the API proxies it to loopback [`ttyd`][ttyd] inside the terminal sidecar after authentication. A separate authenticated loopback exec server lets the `api` and `worker` sidecars run allowlisted `az`, `kubectl`, `azcopy`, `elastic-blast`, `elb`, and `git` commands without installing those CLIs in their own images.

Advanced operators can still run `az`, `kubectl`, `azcopy`, and `elastic-blast`, but the default research path stays in the UI.

## Self-Managed Lifecycle

The control plane can upgrade itself from the browser. Settings &rarr; Updates polls release availability and the Upgrade page drives the upgrade. With native [Container App][azure-container-apps] blue/green enabled (`STRICT_BLUEGREEN`), an upgrade stages a green revision at 0% traffic, health-checks it, cuts traffic over with a confirm window, and keeps the previous blue revision warm so rollback is a seconds-fast traffic-weight flip with no image re-pull. Superseded revisions are garbage-collected. With the flag off, the legacy in-place single-revision recreate runs unchanged.

AKS compute is also self-managed for cost: clusters start on demand and the `beat` reconciler stops them when idle (see [BLAST Job Flow](#blast-job-flow)).

## Why This Shape

- One bundled Container App keeps the steady-state control-plane cost low.
- Sidecars share loopback networking, which removes external Redis and terminal hosts.
- The `api` sidecar reverse-proxies non-`/api/*` requests to the `frontend` sidecar, so the SPA is served same-origin and the browser only ever sees one hostname.
- Celery handles long-running Azure and BLAST operations without blocking HTTP requests.
- Azure Storage is enough for job state, audit history, schedules, and result access; no managed database is required.
- Managed identity keeps downstream Azure access auditable and avoids browser-to-Azure credential forwarding.

What the architecture deliberately does **not** include:

- No managed Cosmos DB / PostgreSQL; state lives in Azure Storage Tables + append blobs.
- No Azure Service Bus in the internal Celery path; the in-revision `redis` sidecar is always the broker. Service Bus is an optional, default-OFF external ingress and event integration only.
- No Azure Static Web App; the `frontend` sidecar serves the React SPA.
- No separate Redis VM or managed Redis service.
- No Remote Terminal VM, no SSH, no admin password; the browser shell is the `terminal` sidecar over a same-origin WebSocket.
- No Azure Functions or Durable Functions; the migration is complete (see [Container Apps Migration](container-apps.md)).
- No SAS tokens issued to the browser; data-plane reads/writes go through the `api` sidecar.

## Go Deeper

- [Container Apps Architecture](container-apps.md) explains the deployed six-sidecar topology, resource list, sizing, and cost reasoning.
- [Auth](authentication.md) covers browser sign-in, backend token validation, managed identity, and RBAC.
- [Browser Terminal](../copilot/browser-terminal.md) describes the terminal sidecar lifecycle and loopback WebSocket model.
- [Resource Plane](../copilot/resource-plane.md) maps Azure preparation and monitoring work to Celery tasks.
- [API Reference](../user-guide/api-reference.md) explains how to call the OpenAPI execution surface from the browser or an external client.

[aks]: https://learn.microsoft.com/azure/aks/what-is-aks
[azure-container-apps]: https://learn.microsoft.com/azure/container-apps/overview
[azure-storage]: https://learn.microsoft.com/azure/storage/common/storage-introduction
[celery]: https://docs.celeryq.dev/en/stable/
[managed-identity]: https://learn.microsoft.com/entra/identity/managed-identities-azure-resources/overview
[msal]: https://learn.microsoft.com/entra/msal/javascript/browser/
[openapi]: https://www.openapis.org/
[private-endpoints]: https://learn.microsoft.com/azure/private-link/private-endpoint-overview
[redis]: https://redis.io/docs/latest/
[service-bus]: https://learn.microsoft.com/azure/service-bus-messaging/service-bus-messaging-overview
[ttyd]: https://github.com/tsl0922/ttyd
[websocket]: https://developer.mozilla.org/docs/Web/API/WebSockets_API