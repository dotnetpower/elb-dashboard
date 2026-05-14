# Container Apps Migration Plan

## Motivation

The control plane has grown beyond a simple Azure Functions API. It now includes
long-running Azure operations, BLAST job orchestration, dashboard monitoring,
Remote Terminal lifecycle management, AKS proxy behavior, and Durable entity
state. The project needs a documented target architecture before implementation
starts.

## User-facing change

- Added a detailed Container Apps migration plan at `docs/container-apps-migration.md`.
- Linked the plan from the README documentation section.
- No runtime behavior changes.

## API/IaC diff summary

- No API changes.
- No IaC changes.
- The document proposes future Bicep modules for Container Apps, Service Bus,
  state storage, private endpoints, identities, and platform ACR.

## Validation evidence

- Documentation-only change.
- `git diff --check`
