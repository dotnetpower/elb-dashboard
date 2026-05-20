# Submit-to-Cancel handoff

## Motivation

A user who submits a New Search could land on the Results page before the job detail query finishes. During that short handoff, the page already knew the job id but hid the Cancel action because the job row had not hydrated yet.

## User-facing change

After a successful submit, the Results URL now carries the Azure run context needed by job actions. The Results header shows Cancel while the job detail request is still fetching, so a just-submitted search can be cancelled immediately after navigation.

## API/IaC diff summary

No backend API or infrastructure changes. The frontend submit mutation marks the redirect with `submitted=1` and preserves `subscription_id`, `resource_group`, `storage_account`, and `cluster_name` in the submitted job URL. The Results header separates running-timer display from cancel availability.

## Validation evidence

- `cd web && npm run test -- taxonomyFilter.test.ts` — 18 passed, including submit navigation URL coverage.
- `cd web && npm run build` — TypeScript and Vite production build passed.
