# Monitoring service SRP extraction

## Motivation

`api/services/monitoring.py` had grown into a mixed-responsibility module:

* ARM-backed AKS summaries;
* direct Kubernetes API session handling and K8s resource readers;
* BLAST job status/cancel helpers;
* Storage, ACR, legacy VM, and resource creation helpers.

The direct Kubernetes section was large enough to hide behavior changes and made
focused linting/testing hard. This change separates the Kubernetes API boundary
from the Azure resource monitoring facade while preserving existing imports.

## User-facing change

No route path or response contract intentionally changes. The dashboard and
BLAST tasks still import Kubernetes helpers through `api.services.monitoring`,
but the implementation now lives in a dedicated module.

Reliability and observability benefits:

* K8s session credential-material cleanup is isolated in one module;
* BLAST status and cancel helpers remain scoped by `job_id` / `elb-job-id`;
* K8s parsing helpers are smaller and easier to test independently;
* `monitoring.py` now reads as an Azure ARM/Storage/ACR facade instead of a
  catch-all service file.

## API / code diff summary

* Added `api/services/k8s_monitoring.py`
  * Owns `_get_k8s_session` and all `k8s_*` helpers.
  * Keeps temp kubeconfig-derived files mode `0600` and deletes them on
    session close or partial setup failure.
  * Keeps BLAST status/cancel scoping by `BLAST_ELB_JOB_ID` and `elb-job-id`.
* Rebuilt `api/services/monitoring.py`
  * Keeps ARM-backed AKS, Storage, ACR, legacy VM, and resource creation
    helpers.
  * Re-exports the K8s helpers through `__all__` for compatibility with
    existing route/task imports.
* Updated `api/tests/test_blast_tasks.py`
  * Patches the extracted `api.services.k8s_monitoring._get_k8s_session` while
    asserting compatibility through `api.services.monitoring.k8s_cancel_blast_job`.

## Validation evidence

```
$ cd /home/moonchoi/dev/elb-dashboard && uv run ruff check api/services/monitoring.py api/services/k8s_monitoring.py api/tasks/blast.py api/tests/test_blast_tasks.py
All checks passed!

$ cd /home/moonchoi/dev/elb-dashboard && uv run pytest -q api/tests/test_blast_tasks.py
.........                                                                [100%]
9 passed in 1.82s

$ cd /home/moonchoi/dev/elb-dashboard && uv run python -m py_compile api/services/monitoring.py api/services/k8s_monitoring.py api/tasks/blast.py
exit=0

$ cd /home/moonchoi/dev/elb-dashboard && uv run pytest -q api/tests
........................................................................ [ 72%]
............................                                             [100%]
100 passed in 10.62s

$ cd /home/moonchoi/dev/elb-dashboard && git diff --check
exit=0
```
