---
title: Architecture
description: Architectural overview of ElasticBLAST Control Plane — the six-sidecar Azure Container App, authentication, and the in-progress research notes that informed the design.
tags:
  - architecture
---

# Architecture

This section is the architectural map of **ElasticBLAST Control Plane**.

## Core references

- [High Level Architecture](high-level.md) — the shipped six-sidecar Container App, AKS BLAST jobs, and how the browser, storage, and managed identity connect.
- [Container Apps Architecture](container-apps.md) — authoritative reference for the deployed topology, ingress, identity, secrets, and the Azure Functions retirement history.
- [Runtime Plan](runtime-plan.md) — supporting infrastructure (VNet/subnets, private DNS, shared MI + RBAC, Storage rules, AKS plan, post-deploy smoke checklist).
- [Identity Architecture](identity.md) — the two managed identities (shared `id-elb-dashboard-*` and the runtime-created `id-elb-openapi` workload MI), their lifecycle, federated identity credentials, full role-ID matrix, and recovery playbooks.
- [Storage Isolation & Browser ↔ Storage Proxy](storage-contract.md) — the load-bearing security contract: `publicNetworkAccess: Disabled`, no SAS to the browser, streaming proxy through the `api` sidecar.
- [Authentication & Authorization](authentication.md) — MSAL Auth Code + PKCE handshake, managed identity, and the full RBAC role matrix.

## Research notes (in-progress)

These pages capture investigations that informed design decisions. They are *not* user-facing documentation.

- [BLAST Search Space Discovery](../research/blast-searchsp-discovery.md) — how the control plane discovers BLAST databases and `searchsp` metadata.
- [Web BLAST Compatibility Plan](../research/web-blast-compatibility-plan.md) — implementation ledger for Web BLAST scientific compatibility.
