# BLAST Query Preview

## Motivation

The BLAST job execution timeline could show `Could not load input.fa` after the upload step succeeded. The preview was asking the generic job file endpoint for `input.fa`, but that endpoint read from the `results` container while uploaded queries live in the `queries` container.

## User-facing change

The Upload Query step now previews the uploaded FASTA from the actual job query blob. The visible label remains `input.fa`, while the request follows the recorded `query_file` / `query_blob_url` path for the job.

## API / IaC / deployment diff

- Extended `/api/blast/jobs/{job_id}/file` so `input.fa`, `query.fa`, job-scoped `queries/...` paths, and full `queries` blob URLs are read from the `queries` container.
- Added path authorization for explicit `queries/...` previews so a job can only read its own uploaded query path or job-scoped prefixes.
- Updated the frontend timeline preview to pass the real uploaded query blob path separately from the display filename.
- No IaC changes.

## Validation

- `uv run pytest -q api/tests/test_smoke.py -k 'blast_job_file'`
- `uv run ruff check api/routes/stubs.py api/tests/test_smoke.py`
- `npx eslint src/api/blast.ts src/components/BlastFilePreview.tsx src/components/BlastStepTimeline/StepLogSection.tsx src/components/BlastStepTimeline/buildStepLog.ts --max-warnings 0`
- `npm run build`
- Production deploy: API/worker/beat `blast-query-preview-api-20260519064600`, frontend `blast-query-preview-frontend-20260519063836`, revision `ca-elb-control--0000086`.
- Production smoke: `GET /api/blast/jobs/75cc51e4-a875-4c04-93ea-854fa21c6ed9/file?name=input.fa...` returned HTTP 200 with `content_length=528`, `truncated=false`, and `looks_like_fasta=true`.
- Production URL smoke: the same endpoint with a full `https://.../queries/uploads/.../query.fa` value returned HTTP 200 with `content_length=528`, `truncated=false`, and `looks_like_fasta=true`.
- Runtime feature flags remain off for production-only surfaces (`VITE_FEATURE_CUSTOM_DB=false`, `VITE_FEATURE_LAB_TOOLS=false`, `VITE_FEATURE_TERMINAL=false`), and ACR was restored to `publicNetworkAccess=Disabled`, `defaultAction=Deny` after the image build.
