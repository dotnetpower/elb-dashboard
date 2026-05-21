# BLAST result database metadata

## Motivation

The BLAST result header already renders database title, update date, sequence count, letter count, and snapshot metadata, but the result page had started requesting job details with `include_database_metadata=false`. That made the header lose useful database context even though the backend still had the metadata.

## User-facing change

BLAST result pages once again request database metadata in the job detail response, so the header can show DB updated date, sequence count, letter count, and related NCBI-style database fields when available.

## API/IaC diff summary

- Frontend: `BlastResults` state now calls `blastApi.getJob(jobId)` without suppressing database metadata.
- API: no contract change. `/api/blast/jobs/{job_id}` already defaults `include_database_metadata=true`.
- IaC: no changes.

## Validation evidence

- Before the change, `GET /api/blast/jobs/bb61858a-8cb6-4590-a2e3-c144662851f7?include_database_metadata=false` returned no `database_metadata`.
- The default `GET /api/blast/jobs/bb61858a-8cb6-4590-a2e3-c144662851f7` response included `core_nt` metadata: `update_date=2026/05/02`, `number_of_sequences=125619662`, and `number_of_letters=1041443571674`.