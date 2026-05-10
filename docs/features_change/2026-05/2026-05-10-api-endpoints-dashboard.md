# New BLAST API Endpoints & Dashboard Enhancements

**Date**: 2026-05-10

## Motivation

The control plane needed additional API endpoints for BLAST job management
(file preview, cancel, history) and the monitoring dashboard cards needed
richer interactive features (AKS provisioning, storage toggle, ACR image
management).

## Changes

### New API Endpoints (`api/function_app.py`)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/blast/jobs/{job_id}/file` | GET | Preview job input files (input.fa, elastic-blast.ini) with truncation for large FASTA files |
| `/api/blast/jobs/{job_id}/cancel` | POST | Cancel a running BLAST job by terminating the Durable Functions orchestration |
| `?history=1` on status endpoint | GET | Query Durable Functions execution history for debugging |

### Dashboard Cards

| File | Change |
|------|--------|
| `web/src/components/cards/ClusterCard.tsx` | AKS provisioning UI: create/start/stop cluster buttons, node pool info, Kubernetes version, attached ACR display, role assignment status. |
| `web/src/components/cards/StorageCard.tsx` | Container browser with blob count/size, public access toggle with timer, Service Endpoint status display, blob upload/download affordances. |
| `web/src/components/cards/AcrCard.tsx` | Per-image build status, build log excerpts on failure, image tag comparison against expected `IMAGE_TAGS`. |

### New Frontend Components

| File | Purpose |
|------|---------|
| `web/src/components/Breadcrumb.tsx` | Navigation breadcrumb for page hierarchy |
| `web/src/components/GettingStarted.tsx` | Onboarding checklist (temporarily disabled) |
| `web/src/components/KeyboardShortcuts.tsx` | Keyboard shortcut overlay |
| `web/src/components/RefreshRing.tsx` | Auto-refresh countdown indicator |
| `web/src/components/Toast.tsx` | Toast notification system |
| `web/src/hooks/` | Custom React hooks for shared state |

### Supporting Backend Changes

| File | Change |
|------|--------|
| `api/services/compute.py` | AKS management: create/start/stop cluster, role assignments |
| `api/services/keyvault.py` | Enhanced Key Vault operations |
| `api/services/network.py` | VNet/subnet management for AKS and terminal |
| `api/orchestrators/provision_aks.py` | New: AKS cluster provisioning orchestrator |
| `api/orchestrators/delete_blast.py` | New: BLAST job deletion orchestrator |

## Validation

- TypeScript: 0 errors.
- Python: 13 tests pass.
- All new endpoints manually tested via browser.
