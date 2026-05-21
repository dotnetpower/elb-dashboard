# API core_nt submit example

## Motivation

The API Reference `POST /v1/jobs` try-it example used `core_nt`, but the request body did not expose the New Search BLAST command contract clearly and fell back to the OpenAPI service's standard runtime profile.

## User-facing change

The default `POST /v1/jobs` request body now mirrors the New Search core_nt command: `blastn -db core_nt -evalue 0.05 -word_size 28 -max_target_seqs 100 -outfmt 5 -dust yes -soft_masking false -searchsp 32156241807668 -query query.fasta -out results.out`. The API Reference try-it executor continues to call the OpenAPI `/v1/jobs` runtime so the returned `job_id` remains the short OpenAPI id used by `/v1/jobs/{job_id}/status`.

## API/IaC diff summary

- Frontend: updated the curated API Reference submit example to split structured BLAST options from raw extra flags and include `resource_profile: core_nt_safe`.
- Frontend: preserved the API Reference executor path through `/api/aks/openapi/proxy` so `POST /v1/jobs` keeps the OpenAPI job id contract instead of returning a Dashboard job UUID.
- Frontend: aligned the API response contract example so top-level `job_id` is the short OpenAPI id and Dashboard UUID remains in `target.dashboard_job_id`.
- API: no backend changes.
- IaC: no changes.

Runtime note: the OpenAPI runtime currently records `resource_profile: core_nt_safe` but does not yet apply that profile to generated ElasticBLAST config. Applying the sharded, memory-capped runtime policy must happen in the OpenAPI runtime so the API can keep its short `job_id` contract.

## Validation evidence

- `npm --prefix web run test -- --run src/pages/apiReference/spec.test.ts src/hooks/useOpenApiExecutor.test.ts` passed: 2 files, 7 tests.
- `npm --prefix web run lint` passed.
- `npm --prefix web run build` passed with the existing large chunk warning.
- Browser `/docs#ep-post--v1-jobs` request body was confirmed to show the core_nt New Search-equivalent BLAST options.
- Browser API response contract panel was confirmed to show top-level `job_id: "17dfd2825089"` with `job_id_kind: "openapi"`.