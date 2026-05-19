# Dashboard Error Noise

## Motivation

The dashboard showed several old failed BLAST jobs and repeatedly attempted to preview `input.fa` for completed jobs that did not record a query blob path. That produced noisy `/api/blast/jobs/{job_id}/file` 503 responses in production logs and made the active submitted job look like it was still pending.

## User-facing change

Cluster job rows now render a `running` job with `phase=submitted` as Running, while terminal failures still win over transient status values. Upload Query file preview is only requested when the job state contains a real uploaded query blob path.

## API / IaC / deployment diff

- No API or IaC changes.
- Frontend job-state classification now treats canonical status as the active-state source after terminal states are checked.
- Upload Query preview no longer falls back to a guessed `queries/{job_id}/input.fa` path.
- The production workload storage account `elbstg01` was restored to `publicNetworkAccess=Disabled` and `defaultAction=Deny`.

## Validation

- `npx vitest run src/components/cards/ClusterBento/jobMapping.test.ts`
- `npx eslint src/components/cards/ClusterBento/jobMapping.ts src/components/cards/ClusterBento/jobMapping.test.ts src/components/BlastStepTimeline/StepLogSection.tsx --max-warnings 0`
- `npm run build`
- Production storage remediation: `elbstg01` now reports `publicNetworkAccess=Disabled`, `defaultAction=Deny`.
- Production deploy: frontend `dashboard-error-noise-frontend-20260519070935`, revision `ca-elb-control--0000087`.
- Browser verification: dashboard now shows `Network: Private only` and job `75cc51e4...` as `Running` instead of `Pending`.
- Log verification: no new `/api/blast/jobs/{job_id}/file` 503 preview errors appeared after reloading the deployed dashboard.