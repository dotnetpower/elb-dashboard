# BLAST Orchestrator Reliability & Managed Identity Fallback

**Date**: 2026-05-10

## Motivation

The BLAST submit orchestrator had several reliability issues:
1. `elastic-blast status` could report `EXIT_CODE=0` prematurely when `az login`
   failed or the temporary AKS cluster had not been created yet.
2. The orchestrator's `custom_status` did not include per-step results, making it
   impossible for the frontend to show detailed execution progress.
3. Activities failed when the VM's `az login` session expired — no automatic
   fallback to managed identity.
4. Storage network rules with VNet/Service Endpoints caused
   `publicNetworkAccess=Disabled` toggle to break legitimate VNet access.

## Changes

### Backend

| File | Change |
|------|--------|
| `api/orchestrators/submit_blast.py` | Added `steps` dict accumulating per-phase results into `custom_status` and final `output`. Added `MIN_POLLS_BEFORE_COMPLETE = 3` — early "completed" results within the first 90 s are ignored and treated as `running`. |
| `api/activities/blast.py` | Submit activity: managed identity fallback (`az login --identity` if `az account show` fails). Export activity: added `az aks get-credentials`, `az storage blob upload` as azcopy alternative, error detection improved (line-start `ERROR:` + keyword matching). `mem_limit` default changed from `32Gi` to `24Gi` for Standard_D8s_v3 compatibility. |
| `api/services/monitoring.py` | `set_storage_public_access()`: when VNet rules exist, keeps `publicNetworkAccess=Enabled` and only toggles `defaultAction` between `Allow`/`Deny`. Prevents breaking Service Endpoint access. |
| `api/services/storage_data.py` | Added `read_blob_text()` for reading blob content (used by file preview endpoint). |

### Frontend

| File | Change |
|------|--------|
| `web/src/pages/BlastResults.tsx` | GitHub Actions-style collapsible Execution Steps with per-phase detail logs. FASTA syntax highlighting (A=green, T/U=red, G=yellow, C=blue). INI syntax highlighting. Cancel button with ConfirmDialog. Duration display (live timer + final). Results file type badges. Re-submit link. StorageLockedPanel with Enable & Load affordance. |
| `web/src/pages/BlastJobs.tsx` | View button removed; job title click navigates to detail. |

## Validation

- 13 Python tests pass.
- TypeScript compiles with 0 errors.
- BLAST job `job-3c417e30ef44` ran end-to-end with step accumulation visible in UI.
