# DB Get AKS Topology Confirmation

## Motivation

Starting a BLAST database copy before an AKS workload cluster exists is valid, but shard layout selection and node-local warmup are finalized only after the workload node count is known. Users needed an explicit checkpoint before starting DB get in that uncertain state.

## User-facing change

The BLAST Databases modal now shows a centered confirmation dialog when the user clicks Get while no AKS workload cluster is available, AKS status is still loading, AKS status failed to load, or the workload node count is zero/unknown. The message explains that DB copy can continue, but sharding and node-local warmup may take extra time later when the node count becomes known. Users can continue or cancel.

Large database downloads still surface size/time risk in the same confirmation when AKS topology is unknown.

## API / IaC diff summary

- Frontend only.
- Reuses the existing `/api/monitor/aks` query data and the shared workload-pool node-count helper.
- No backend API, storage, or infrastructure changes.

## Validation evidence

- `npm run test -- src/components/cards/storage/BlastDbClusterConfirm.test.ts`
- `npm run build`
- Browser smoke on `http://localhost:8090/` with mocked empty AKS cluster list: clicking `Get` in the BLAST Databases modal rendered `Get 16S ribosomal RNA before AKS is ready?` with `Continue and Get` and `Cancel` actions.
