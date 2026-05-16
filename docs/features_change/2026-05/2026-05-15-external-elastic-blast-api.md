# External ElasticBLAST API Facade

## Motivation

External callers need a small stable API for BLAST submission, status polling, and XML result download without depending on dashboard internals.

## User-facing change

Adds authenticated external API routes:

- `POST /api/v1/elastic-blast/submit`
- `GET /api/v1/elastic-blast/jobs/{job_id}`
- `GET /api/v1/elastic-blast/jobs/{job_id}/files/{file_id}`

The facade forwards to the sibling OpenAPI execution plane, sets `submission_source=external_api`, preserves BLAST+/DB version fields returned by the execution plane, and streams XML downloads.

## API/IaC diff summary

- New `api.routes.elastic_blast` router registered before the frontend catch-all.
- New `api.services.external_blast` client for the sibling OpenAPI service.
- Request validation fixes `outfmt=5`, bounds FASTA payloads, restricts job/file path values, validates program and priority, and sanitises upstream errors.
- `core_nt` is allowed in the warmup task's database registry so the end-to-end external API path can pre-warm the same database used by the direct caller contract.
- Warmup verification now checks that the database exists in workload Storage instead of calling a non-existent `elastic-blast get-blastdb` command.
- No IaC changes.

## Validation evidence

- `uv run ruff check api/routes/elastic_blast.py api/services/external_blast.py api/tests/test_external_blast_api.py` → passed.
- `uv run pytest -q api/tests` → 137 passed.
- Focused dashboard regression after live E2E hardening: `uv run ruff check api/tasks/openapi.py api/tasks/storage.py api/routes/elastic_blast.py api/services/external_blast.py api/tests/test_external_blast_api.py api/tests/test_smoke.py` → passed; `uv run pytest -q api/tests/test_external_blast_api.py api/tests/test_smoke.py` → 37 passed.
- Sibling execution-plane focused validation: `pytest -q tests/openapi/test_queue.py` → 18 passed.
- Repeated severity review until no Medium/Critical/High findings remained; only Low/nits remain.
- Live Azure E2E against warmed `core_nt` via the dashboard facade and sibling OpenAPI:
	- DB warmup: `core_nt` present in workload Storage with 799 existing files plus `taxdb.btd`/`taxdb.bti`; AKS blast pool nodes warmed through `warm-core-nt-0..2` jobs.
	- OpenAPI image validated in AKS: `elbacr01.azurecr.io/elb-openapi:4.8`; job-submit image `elbacr01.azurecr.io/ncbi/elasticblast-job-submit:4.1.0` rebuilt with final digest `sha256:fe2c2e4c902878746adccb4beb72b31a78bd1b35bd748b1d2096cb50b1599b0a`.
	- Run 1: `8fea24494cba` (`f3l-inclusive-e2e-17`) reached external status `success`; `GET /api/v1/elastic-blast/jobs/8fea24494cba/files/result-001` downloaded a 7462-byte gzipped XML file containing `Hit_def=Monkeypox virus isolate 24MPX1702C_ont genome assembly, complete genome: monopartite`.
	- Run 2: `c010f171c142` (`f3l-inclusive-e2e-18`) reached external status `success`; result file `result-001` reported `size_bytes=7462` and downloaded successfully with the same F3L hit.
	- Run 3: `0b087d25ce3d` (`f3l-inclusive-e2e-19`) reached external status `success`; result file `result-001` reported `size_bytes=7462` and downloaded successfully with the same F3L hit.
	- Provenance fields verified in live payload: `blast_version=2.17.0+` from result XML and `db_version=2026-05-09-01-05-02` from `core_nt-nucl-metadata.json`.
- Post-validation cleanup: workload Storage `elbstg01` restored to `publicNetworkAccess=Disabled`, `defaultAction=Deny`, `ipRules=[]`.