# BLAST Pre-flight Readiness Checks

**Date**: 2026-05-11

## Motivation

BLAST job submission could fail deep into the orchestration (step 5 of 8) when preconditions weren't met: ACR images not built, BLAST database not downloaded, AKS cluster stopped, Terminal VM not running, storage containers missing, or invalid FASTA. Users had no way to know what was wrong before clicking Submit.

## User-facing Change

- New "Check Readiness" button in the BLAST Submit page footer (appears when basic form requirements are met)
- Runs 6 server-side checks and displays a checklist card with pass/fail/warn status:
  1. **ACR images built** — lists missing images with "Build from Dashboard" action
  2. **BLAST database exists** — if missing, suggests `core_nt`, `16S_ribosomal_RNA`, `nt`, `nr`, `swissprot` with one-click selection
  3. **AKS cluster running** — shows power state, links to Dashboard
  4. **Terminal VM running** — links to Terminal page for provisioning
  5. **Storage containers** — checks blast-db, queries, results exist
  6. **FASTA format** — validates `>` header, counts sequences and residues

## API Diff

### New endpoint: `POST /blast/pre-flight`

Request body:
```json
{
  "subscription_id": "...",
  "resource_group": "...",
  "acr_resource_group": "...",
  "acr_name": "...",
  "storage_account": "...",
  "aks_cluster_name": "...",
  "terminal_resource_group": "...",
  "terminal_vm_name": "...",
  "db": "blast-db/core_nt/core_nt",
  "query_data": ">seq1\nATGC..."
}
```

Response:
```json
{
  "ready": false,
  "checks": [
    {"id": "acr_images", "status": "fail", "title": "ACR images not built", "detail": "Missing: ncbi/elb:1.4.0", "action": "Build images from Dashboard", "severity": "critical"},
    {"id": "blast_db", "status": "fail", "title": "Database 'core_nt' not found", "suggested_dbs": ["core_nt", "16S_ribosomal_RNA", "nt", "nr", "swissprot"], "action_type": "download_db"},
    {"id": "aks_cluster", "status": "pass", "title": "AKS cluster 'elb-cluster-0509' running"},
    {"id": "terminal_vm", "status": "pass", "title": "Terminal VM running"},
    {"id": "storage_containers", "status": "pass", "title": "Storage containers ready"},
    {"id": "fasta_format", "status": "pass", "title": "FASTA valid: 1 sequence(s), 32 residues"}
  ],
  "critical_blockers": 2,
  "summary": "2 critical issue(s) must be resolved before submitting"
}
```

### Frontend

| File | Change |
|------|--------|
| `web/src/api/endpoints.ts` | Added `blastApi.preFlight()` typed client |
| `web/src/pages/BlastSubmit.tsx` | Added `preFlightMutation`, "Check Readiness" button, checklist result card with suggested DB quick-select |

## Validation

- TypeScript build: 0 errors
- Python syntax check: OK
- 13 unit tests pass
