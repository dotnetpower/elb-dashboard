# OpenAPI status and result repair

## Motivation

Live OpenAPI validation showed that a successful external ElasticBLAST submission could report `running` at 99% after all BLAST shards had finished, and the execution summary could include unrelated historical Kubernetes Jobs. The root cause was that the OpenAPI service did not persist the actual ElasticBLAST correlation id (`job-...`) when `elastic-blast submit` did not emit JSON. Status refresh then queried `elb-job-id=<dashboard_job_id>`, found nothing, and fell back to aggregating every `app=blast|submit|finalizer` Job in the namespace.

## User-facing change

External OpenAPI `submit`, `status`, and result download now report the job-scoped execution state. Completed jobs return `status=success`, `execution.shard_count` for the actual ElasticBLAST run, and downloadable result files without mixing unrelated cluster history.

## API / runtime diff summary

- `scripts/dev/patch-openapi-build-context.py` now copies required OpenAPI build support files into the patched build context so ACR builds do not fail on Dockerfile `COPY` instructions.
- The OpenAPI runtime patch extracts the actual ElasticBLAST `job-...` correlation id from submit stdout when JSON output is absent.
- Status refresh now checks terminal markers under both the historical root metadata path and the actual `results/<dashboard_job_id>/<elb_job_id>/metadata/` path.
- Unsafe namespace-wide Kubernetes Job/Pod fallback aggregation was removed from the patched OpenAPI runtime.
- Terminal external job payloads repair stale persisted summaries after pod restart by recalculating `k8s_summary` with the recovered `elb_job_id`.

## Validation evidence

- Reproduced live issue on `rg-elb-01/elb-cluster` with job `5a4ae5b100ad`: status initially showed `running`, `progress_pct=99`, `shard_count=100`, and `/v1/jobs/5a4ae5b100ad/results` returned `No result files found` while the actual finalizer was still tracked under `elb-job-id=job-ba27e40b527440c5a94cf617df5f5b53`.
- Patched and compiled a temporary sibling `docker-openapi` build context with `python -m py_compile /tmp/docker-openapi-patch-test/app/main.py`.
- Built and pushed `elbacr01.azurecr.io/elb-openapi:4.9` digest `sha256:ccc0bf512ce92b3b9381e081fab6e76ff9aed224d54bace68a96a9aec33c1dc4` from ACR build run `de1p`.
- Rolled `deployment/elb-openapi` and verified the running pod contains `_discover_elb_job_id_from_submit_output`, terminal summary repair, and no unsafe `kubectl get jobs -o json` fallback.
- Rechecked job `5a4ae5b100ad`: status returned `success`, `execution={shard_count:1, shards_succeeded:1, shards_active:0, shards_failed:0}`, and ConfigMap `elb_job_id` was repaired to `job-ba27e40b527440c5a94cf617df5f5b53`.
- Ran 10 live OpenAPI rounds using idempotent submit replay of the completed probe: every round returned submit HTTP 202, status HTTP 200, result download HTTP 200, one result file `batch_000-blastn-16S_ribosomal_RNA.out.gz`, 740 bytes, gzip media type, and BLAST XML prefix validation passed.
