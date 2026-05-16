# BLAST task hardening

## Motivation

BLAST submission is the highest-risk workflow in the control plane: it bridges
browser requests, Celery retries, Table-backed job state, the terminal sidecar,
ElasticBLAST's Azure adapter, AKS jobs, and private Storage. The previous task
implementation had several reliability and observability gaps:

* state updates used the wrong `JobStateRepository.update(...)` calling
  convention, so progress could be silently lost;
* config was written with `bash -c`, but the terminal exec server only allows
  audited binaries such as `elastic-blast`, `azcopy`, `kubectl`, `az`, and
  `elb`;
* generated config did not bind the requested AKS cluster name, query URL,
  database URL, or results root correctly;
* transient terminal/capacity failures were converted into terminal `failed`
  results instead of Celery retries;
* status checks used the ElasticBLAST CLI config path even though the CLI does
  not re-apply the idempotency key for status/delete.

## User-facing change

BLAST jobs now surface clearer, more recoverable lifecycle state:

* submit pipes the INI config through `elastic-blast submit --cfg - --json`;
* `job_id` is passed as both idempotency key and correlation id, so Celery
  retries can safely resume the same ElasticBLAST submission;
* transient terminal-sidecar and ElasticBLAST capacity failures schedule a
  retry and write a `retry_scheduled` history event;
* successful/running phases clear stale `error_code` values;
* status checks use the direct Kubernetes API helper scoped by
  `BLAST_ELB_JOB_ID`, avoiding cross-job status bleed on shared clusters;
* cancel deletes Kubernetes Jobs with `app in {blast, submit}` and
  `elb-job-id = job_id`, avoiding the ElasticBLAST CLI delete path's
  in-process `elb_job_id` reconstruction gap;
* relative query/database paths reject `..` traversal before a config is sent
  to the terminal sidecar.

## API / task diff summary

* `api/tasks/blast.py`
  * Replaced shell temp-file config writes with `stdin` to `elastic-blast`.
  * Added URL normalization for `queries`, `blast-db`, and `results` Storage
    roots using Azure `https://<account>.blob.core.windows.net/...` URLs.
  * Added structured JSON tail parsing for ElasticBLAST Azure adapter output.
  * Added retry classification for `transient`, `capacity`, and `conflict`
    categories plus known retryable ElasticBLAST exit codes.
  * Fixed state/history writes to call `JobStateRepository.update(job_id, ...)`
    and `append_history(job_id, event, payload)` correctly.
  * Switched `check_status` to `k8s_check_blast_status(..., job_id=job_id)`.
  * Switched `cancel` to `k8s_cancel_blast_job(..., job_id=job_id)`.
* `api/services/k8s_monitoring.py` via the `api.services.monitoring` facade
  * Added `k8s_cancel_blast_job`, which uses the direct Kubernetes API to
    delete only Jobs labelled with the current `elb-job-id`.
* `api/tests/test_blast_tasks.py`
  * Added focused regression coverage for config generation, stdin argv shape,
    structured JSON parsing, retry classification, traversal rejection, and
    state repository / K8s cancellation contracts.

## Validation evidence

```
$ cd /home/moonchoi/dev/elb-dashboard && uv run ruff check api/tasks/blast.py api/tests/test_blast_tasks.py
All checks passed!

$ cd /home/moonchoi/dev/elb-dashboard && uv run pytest -q api/tests/test_blast_tasks.py
.........                                                                [100%]
9 passed in 0.58s

$ cd /home/moonchoi/dev/elb-dashboard && uv run python -m py_compile api/services/monitoring.py api/services/k8s_monitoring.py
exit=0

$ cd /home/moonchoi/dev/elb-dashboard && uv run pytest -q api/tests
........................................................................ [ 72%]
............................                                             [100%]
100 passed in 9.46s
```
