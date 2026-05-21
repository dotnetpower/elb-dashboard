# First-run readiness guide

## Motivation

The first-run setup wizard selects the Azure workspace, Storage account, and ACR, but that does not make the deployment ready to run BLAST. A practical first search also needs prepared BLAST databases in Storage, shard layouts, AKS capacity, and warmup.

## User-Facing Change

- Updated Get Started to separate workspace setup from BLAST runtime readiness.
- Updated the in-app Getting Started guide copy so the database step covers NCBI database preparation, shard layout preparation, and AKS warmup.
- Clarified that the setup wizard is the resource-selection gate and the Dashboard readiness flow is the operational gate before New Search.
- Added screenshots and explanation for creating AKS from Cluster Plane and preparing BLAST databases with the Get action.

## API/IaC Diff Summary

- No API or infrastructure changes.
- Frontend copy only in the Dashboard Getting Started guide.
- Documentation copy only in Get Started.
- Dashboard user-guide screenshots only; no runtime behaviour change.

## Validation Evidence

- `uv run mkdocs build` passed.
- `cd web && npm run build` passed with the existing Vite large chunk warning.