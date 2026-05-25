# Topbar Active Job Status

## Motivation

The topbar BLAST job chip could keep showing an older completed job while the user was viewing a newly submitted job detail page whose live status was still `submitting`.

## User-facing change

On BLAST job detail routes, the topbar chip now prefers the active detail job status. The global latest-job chip is still used elsewhere in the dashboard.

## API/IaC diff summary

No API or IaC changes. The frontend reuses the existing `/api/blast/jobs/{job_id}` query cache for the active detail page.

## Validation evidence

- `npm run test -- useLatestBlastJob`
- `npm run build`