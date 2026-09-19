---
title: Architecture
description: Architectural overview of ElasticBLAST Control Plane — the six-sidecar Azure Container App, authentication, and the in-progress research notes that informed the design.
tags:
  - architecture
---

# Architecture

This section is the architectural map of **ElasticBLAST Control Plane**.

## Core references

- [High Level Architecture](high-level.md) — browser and optional queue ingress, the shipped six-sidecar Container App, private Storage, and AKS execution.
- [Container Apps Architecture](container-apps.md) — authoritative reference for the deployed topology, ingress, identity, secrets, and the Azure Functions retirement history.
- [Runtime Plan](runtime-plan.md) — supporting infrastructure (VNet/subnets, private DNS, shared MI + RBAC, Storage rules, AKS plan, post-deploy smoke checklist).
- [Identity Architecture](identity.md) — the two managed identities (shared `id-elb-dashboard-*` and the runtime-created `id-elb-openapi` workload MI), their lifecycle, federated identity credentials, full role-ID matrix, and recovery playbooks.
- [Storage Isolation & Browser ↔ Storage Proxy](storage-contract.md) — the load-bearing security contract: `publicNetworkAccess: Disabled`, no SAS to the browser, streaming proxy through the `api` sidecar.
- [Authentication & Authorization](authentication.md) — MSAL Auth Code + PKCE handshake, managed identity, and the full RBAC role matrix.
- [Service Bus BLAST Integration](service-bus-integration.md) — optional request queue, execution admission, completion events, DLQ handling, and durable outbox contract.
- [Service Bus Examples](service-bus-examples.md) — standalone producer, monitor, and consumer examples for the external integration.
- [Diagnostics](diagnostics.md) — read-only reliability, availability, and security checks surfaced by the Diagnostics page.

## Research notes (in-progress)

These pages capture investigations that informed design decisions. They are *not* user-facing documentation.

- [BLAST Search Space Discovery](../research/blast-searchsp-discovery.md) — how the control plane discovers BLAST databases and `searchsp` metadata.
- [Web BLAST Compatibility Plan](../research/web-blast-compatibility-plan.md) — implementation ledger for Web BLAST scientific compatibility.
- [AKS Capacity Gate](../research/aks-capacity-gate.md) — historical Redis capacity-gate design plus current implementation and supersession status.
- [Cross-Path Submit Coordination](../research/blast-submit-coordination.md) — implemented Kubernetes Lease and active-job-count coordination shared with OpenAPI.
- [Unbounded Socket Timeouts](../research/unbounded-socket-timeouts.md) — postmortem and bounded-timeout design rules for external network calls.
