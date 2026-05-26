---
title: Glossary
description: Definitions for Azure, identity, networking, Kubernetes, BLAST, and project-specific abbreviations used across the ElasticBLAST Control Plane documentation.
hide:
  - toc
tags:
  - overview
---

# Glossary

A quick reference for the abbreviations and product names used across this documentation. Grouped by domain so a reader unfamiliar with Azure or Kubernetes can land on the right concept fast. The bold expansion is the canonical name; the body is a one-line definition with the project-specific role this control plane assigns to it.

## Azure platform

AKS
:   **Azure Kubernetes Service** — Microsoft's managed Kubernetes offering. The control plane provisions an AKS cluster to run the actual BLAST workload jobs (split, search, merge). The dashboard never SSHes into nodes; it talks to AKS through the [Kubernetes API](https://learn.microsoft.com/azure/aks/intro-kubernetes).

ACR
:   **Azure Container Registry** — Private Docker image registry. Hosts the `frontend`, `api`, `worker`, `beat`, `terminal`, and `elb-*` BLAST runtime images that the Container App and AKS pull. Built with `az acr build` from `postprovision.sh`.

ACA / Container Apps
:   **Azure Container Apps** — Serverless container hosting on top of Kubernetes + Envoy. The dashboard runs as a single Container App (`ca-elb-dashboard`) with six sidecars in one revision. See [Azure Container Apps overview](https://learn.microsoft.com/azure/container-apps/overview).

ARM
:   **Azure Resource Manager** — The Azure control plane that creates, reads, updates, and deletes resources. All `az ...` commands and Bicep deployments go through ARM. The `api` sidecar proxies a subset under `/api/arm/*`.

azd
:   **Azure Developer CLI** — Single command (`azd up`) that provisions Bicep, runs `az acr build`, and applies the Container App template. The repo's primary deploy tool — see [Get Started](get-started.md).

Bicep
:   Microsoft's declarative DSL for ARM templates. All infrastructure in this repo lives in [`infra/`](https://github.com/dotnetpower/elb-dashboard/tree/main/infra) as `*.bicep` modules — there are no hand-written ARM JSON templates.

Entra / Entra ID
:   **Microsoft Entra ID** (formerly Azure Active Directory / AAD). The identity provider that issues access tokens to the browser via MSAL and authenticates the managed identity to ARM and Storage.

Key Vault
:   **Azure Key Vault** — Stores App Registration values and other secrets. The Container App reads them as Key Vault references; the repo never commits secrets.

Storage Account
:   **Azure Storage** — Holds BLAST queries (input), results (output), the `jobstate` Table that backs the state repository, append-blob audit logs, and append-blob command history. Always deployed with `publicNetworkAccess: Disabled`; reachable only through private endpoints from the Container App's VNet. See the [storage contract](architecture/storage-contract.md).

Table Storage
:   The NoSQL row store inside an Azure Storage account. Used here instead of a managed database (Cosmos DB, PostgreSQL) for cost reasons — job state, schedules, and audit rows fit the row/append shape.

App Service / App Registration
:   **Microsoft Entra App Registration** — The identity object that defines the SPA's client ID, redirect URIs, and the API scope MSAL requests. Created (or reused) at deploy time. Not to be confused with *Azure App Service*, which this project does **not** use.

App Insights
:   **Azure Application Insights** — Telemetry sink for traces, exceptions, and request logs. The api sidecar emits structured logs; severe errors surface there first.

## Identity & auth

MI
:   **Managed Identity** — An Entra identity issued and rotated by Azure, attached to a resource, with no client secret to store. The shared user-assigned identity `id-elb-dashboard-*` is what the api/worker/beat/terminal sidecars use for every Azure SDK call. See [Microsoft Learn: managed identities](https://learn.microsoft.com/entra/identity/managed-identities-azure-resources/overview).

UAMI
:   **User-Assigned Managed Identity** — A managed identity created as a standalone resource and attached to one or more workloads. This deployment uses exactly one UAMI shared across all six sidecars. The opposite is *system-assigned*, which is bound 1:1 to a single resource and dies with it.

SAMI
:   **System-Assigned Managed Identity** — A managed identity whose lifecycle is bound to its host resource. *Not used* by this control plane; called out only so readers don't confuse the two.

MSAL
:   **Microsoft Authentication Library** — Microsoft's client-side OAuth/OIDC library. The SPA uses [`@azure/msal-browser`](https://learn.microsoft.com/entra/identity-platform/msal-overview) to sign the user in with Auth Code + PKCE; the api validates the resulting bearer token.

OAuth 2.0 / OIDC
:   **OAuth 2.0** is the delegated-access protocol; **OpenID Connect** layers identity (the `id_token`) on top. MSAL implements both. See [identity reference](architecture/identity.md).

PKCE
:   **Proof Key for Code Exchange** — The OAuth 2.0 extension that lets browser SPAs use the Authorization Code flow safely without a client secret. Required by Entra for SPA app registrations.

JWT
:   **JSON Web Token** — The signed-token format MSAL hands the browser. The api validates the JWT (issuer, audience, signature, expiry) on every request — see [api/auth.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/auth.py).

RBAC
:   **Role-Based Access Control** — Azure's permission system. The managed identity is granted roles like `Storage Blob Data Contributor` and `AcrPull` on specific scopes; nothing is granted via SAS or shared keys.

SP
:   **Service Principal** — A non-human Entra identity backed by a client secret or certificate. The project deliberately avoids SPs in favour of managed identities — no secrets to rotate, no `.env` leaks.

OBO
:   **On-Behalf-Of flow** — An OAuth pattern where a backend exchanges a user's token for another token to call a downstream API as that user. *Not used* here — the backend calls Azure as the shared MI, not as the signed-in user.

SAS
:   **Shared Access Signature** — A pre-signed Azure Storage URL granting time-limited access. **Never issued to the browser by this control plane.** All blob I/O is proxied through the api sidecar so the Storage account can stay `publicNetworkAccess: Disabled`.

dev-bypass / `AUTH_DEV_BYPASS`
:   Local-only environment flag that lets the api accept requests without a valid MSAL token. Off by default and refused in any deployed Container App — see [`scripts/dev/local-debug-auth.sh`](https://github.com/dotnetpower/elb-dashboard/blob/main/scripts/dev/local-debug-auth.sh).

## Networking

VNet
:   **Virtual Network** — Azure's private network primitive. The Container App and AKS run inside one VNet; Storage and ACR are reached through private endpoints in the same VNet.

Subnet
:   An IP range within a VNet. The deployment carves out subnets for the Container Apps Environment, AKS nodes, and private endpoints.

PE / Private Endpoint
:   A private IP inside a VNet that fronts a PaaS resource (Storage, ACR, Key Vault). Lets the Container App reach Storage without the public internet, and lets Storage keep `publicNetworkAccess: Disabled`. See [Microsoft Learn: private endpoints](https://learn.microsoft.com/azure/private-link/private-endpoint-overview).

NSG
:   **Network Security Group** — Azure firewall rules attached to a subnet or NIC. Used sparingly here; the Container App has no public NSG because ingress is fronted by the platform's Envoy.

DNS / Private DNS Zone
:   Resolves the `*.blob.core.windows.net` / `*.azurecr.io` names to their private endpoint IPs inside the VNet, so SDK calls transparently use the private path.

FQDN
:   **Fully Qualified Domain Name** — e.g. `ca-elb-dashboard.<region>.azurecontainerapps.io`. The dashboard's public origin; one FQDN per Container App.

TLS
:   **Transport Layer Security** — All public ingress is HTTPS/TLS, terminated by the Container Apps platform.

CORS
:   **Cross-Origin Resource Sharing** — Browser security mechanism for cross-origin requests. The SPA and api share an origin (the api sidecar reverse-proxies the frontend sidecar), so CORS is not in the hot path; it only matters for the optional external OpenAPI surface.

WebSocket / WSS
:   The bidirectional protocol the browser terminal uses (`wss://…/api/terminal/ws`). The api sidecar validates the MSAL token on the handshake, then proxies the stream to the loopback ttyd in the `terminal` sidecar. See [browser terminal reference](copilot/browser-terminal.md).

CIDR
:   **Classless Inter-Domain Routing** — IP-range notation like `10.42.0.0/16`. Used in Bicep to size VNet and subnet ranges.

## Kubernetes

K8s
:   Short-form for **Kubernetes**. AKS is Azure's managed K8s; we use the upstream Kubernetes API for all cluster reads/writes — never `az aks command invoke` / `begin_run_command`.

Pod
:   The smallest deployable unit in Kubernetes — one or more containers sharing the same network namespace. BLAST split / search / merge each run as Pods (managed by Jobs) on the AKS workload pool.

Job
:   A Kubernetes workload that runs Pods to completion. ElasticBLAST submits search work as a `Job` per shard.

Deployment / StatefulSet
:   Long-running Pod controllers in Kubernetes. The `elb-openapi` service runs as a Deployment on AKS.

PVC
:   **PersistentVolumeClaim** — A K8s request for durable storage, backed in AKS by an Azure Disk or Azure Files share. Used by the warmup pods that stage BLAST databases.

Ingress
:   The K8s object that exposes services over HTTP(S). The control plane itself does *not* expose AKS to the internet — only Container Apps has public ingress.

kubeconfig
:   The credentials file that lets a client talk to the K8s API. The api sidecar builds an in-memory kubeconfig using the managed-identity token; it is never written to disk.

## BLAST domain

BLAST
:   **Basic Local Alignment Search Tool** — NCBI's sequence-similarity search. The workload this control plane orchestrates. See [NCBI BLAST](https://blast.ncbi.nlm.nih.gov/Blast.cgi).

ElasticBLAST
:   NCBI's distributed BLAST runner that schedules shards on Kubernetes (or AWS Batch). The browser terminal carries the `elastic-blast` CLI; the dashboard wraps `elastic-blast submit / status / delete`. See the [ElasticBLAST docs](https://blast.ncbi.nlm.nih.gov/doc/elastic-blast/index.html).

NCBI
:   **National Center for Biotechnology Information** — Maintains BLAST, ElasticBLAST, and the public sequence databases the control plane stages.

OpenAPI / Swagger
:   The machine-readable HTTP API specification format. The api sidecar exposes `/docs` (Swagger UI) and `/openapi.json`; the BLAST OpenAPI execution path also calls an `elb-openapi` service deployed to AKS.

SSE
:   **Server-Sent Events** — One-way HTTP streaming used in some research-plan endpoints. The active job-event stream uses polled JSON instead; SSE is called out so the research notes are clear.

## Project-specific

Control plane
:   The dashboard itself — sidecars in `ca-elb-dashboard` plus the Azure resources they manage. *Not* where BLAST search runs; that's the AKS workload plane.

Workload plane
:   The AKS cluster where ElasticBLAST jobs actually execute. The dashboard creates, monitors, scales, and tears it down but never runs application code there itself.

Sidecar
:   A container that runs in the same Kubernetes Pod / Container App revision as the main app and shares its network namespace. This deployment uses six sidecars (`frontend`, `api`, `worker`, `beat`, `redis`, `terminal`) in one revision so they can talk over `127.0.0.1`.

ttyd
:   The open-source TTY-over-WebSocket bridge ([tsl0922/ttyd](https://github.com/tsl0922/ttyd)) running inside the `terminal` sidecar on loopback `127.0.0.1:7681`. The api sidecar is the only client; it never binds a public address.

Celery
:   Distributed Python task queue. Long-running operations (BLAST submit/delete, ACR builds, AKS provisioning, DB warmup) are dispatched as Celery tasks to the `worker` sidecar via the in-revision Redis broker. See [Celery docs](https://docs.celeryq.dev/).

beat
:   The Celery scheduler sidecar. Drives periodic reconciliation (queue rebuild from `jobstate`, schedule expansion).

Redis
:   In-memory key/value store. Runs as a sidecar (`redis:7-alpine`) and is the Celery broker. State is intentionally **ephemeral** — the beat reconciler rebuilds the queue from Azure Table Storage on revision restart. See [Redis](https://redis.io/docs/latest/).

SPA
:   **Single-Page Application** — The browser frontend (`web/`, React + Vite + TypeScript). Served by the `frontend` sidecar at `127.0.0.1:8081` and proxied to the public origin by the api sidecar.

CLI
:   **Command-Line Interface**. In this repo, "the CLI" usually means the `elastic-blast` binary inside the `terminal` sidecar (the user-facing one) or `az` / `kubectl` / `azcopy` (the operator-facing ones). Researchers should not need any of them locally.

azcopy
:   Microsoft's high-throughput Storage transfer tool. Lives in the `terminal` sidecar (not in api/worker) and is invoked through the loopback exec server when needed for large query/result staging.

elb-openapi
:   The optional ElasticBLAST OpenAPI service deployed to AKS. Exposes a REST surface in front of the BLAST databases; the dashboard's New Search and API Reference can route through it.

IaC
:   **Infrastructure as Code** — All Azure resources for this project are described in Bicep under [`infra/`](https://github.com/dotnetpower/elb-dashboard/tree/main/infra). No portal-only edits.

SemVer
:   **Semantic Versioning** (`MAJOR.MINOR.PATCH`). This repo uses `A.B.<build>` where build is the commit count since the last `vA.B.0` tag. Always bump versions via [`scripts/dev/bump-version.sh`](https://github.com/dotnetpower/elb-dashboard/blob/main/scripts/dev/bump-version.sh) — see [version management](copilot/version-management.md).

## See also

- [High Level Architecture](architecture/high-level.md) — where these pieces fit together.
- [Identity reference](architecture/identity.md) — how MSAL, the App Registration, and the UAMI line up.
- [Storage contract](architecture/storage-contract.md) — why the SAS-free, proxy-through-api rule exists.
- [Tags](tags.md) — find every page that touches a given domain (auth, infra, blast, …).
