# Stage 2 — terminal sidecar per-job workdir isolation (issue #23)

## Motivation

Issue #23's Stage 2 calls for "per-job workdir isolation" in the terminal
sidecar so two parallel `elastic-blast submit` invocations on the same
cluster do not corrupt each other's `elastic-blast.ini` or staged files.
The design doc ([docs/research/aks-capacity-gate.md §4.2](../../research/aks-capacity-gate.md))
specifically proposes routing each submit through
`~/elb-runs/<job_id>/elastic-blast.ini` with an explicit `--cwd`.

## User-facing change

None. This is a verification + documentation pass — the existing code already
satisfies (in a stronger form) the isolation guarantee Stage 2 asks for.

## Code diff summary

* No production code change.
* New test [api/tests/test_terminal_exec_workdir.py](../../../api/tests/test_terminal_exec_workdir.py)
  asserts that the exec server's per-request `tempfile.mkdtemp` contract
  is what the gate-eligible call sites rely on (two parallel
  `_make_cwd(None)` calls must produce different paths).

## Why no code change

The terminal exec server already provides **per-request** workdir
isolation, which is strictly stronger than per-job:

* [terminal/exec_server.py](../../../terminal/exec_server.py)
  `_make_cwd(explicit=None)` → `tempfile.mkdtemp(prefix="req-<uuid>-", dir=/tmp/exec)`
  for every `/exec/stream` and `/exec/run` invocation that doesn't pass an
  explicit `cwd`.
* The matching `finally` branch runs `shutil.rmtree(cwd, ignore_errors=True)`
  on success, failure, and timeout — confirmed at
  [terminal/exec_server.py](../../../terminal/exec_server.py) `_run_owned` /
  `_stream_owned`.
* `api/services/terminal_exec.py::stream(... cwd=None)` is what
  `api/tasks/blast/submit_runtime.py::_stream_submit_command` calls today,
  so each `elastic-blast submit` lands in its own throwaway `/tmp/exec/req-XXXX/`.

Two parallel submits on the same cluster therefore cannot collide on
`elastic-blast.ini`, the staged query file, or any other file written into
`cwd` — they each get a fresh dir.

## What about K8s object collisions?

The Stage 2 issue body also asks: "Confirm K8s object names (Job / SA /
Secret / PVC) include `job_id` so two parallel submits to the same
`cluster_name` don't collide."

Audit of the sibling repo:

* [elastic-blast-azure/src/elastic_blast/elb_config.py](../../../../elastic-blast-azure/src/elastic_blast/elb_config.py)
  generates `elb_job_id = 'job-' + uuid.uuid4().hex` per submission
  (`field(default_factory=...)`).
* Every K8s manifest under
  [elastic-blast-azure/src/elastic_blast/templates/](../../../../elastic-blast-azure/src/elastic_blast/templates/)
  stamps `elb-job-id: "${BLAST_ELB_JOB_ID}"` as a metadata label and a
  selector label, and the runtime delete / count helpers in
  [`azure_api.py`](../../../../elastic-blast-azure/src/elastic_blast/azure_api.py)
  + [`kubernetes.py`](../../../../elastic-blast-azure/src/elastic_blast/kubernetes.py)
  scope every `kubectl` call to that label — so two simultaneous
  submissions on the same namespace remain isolated **for label-driven
  queries**.
* **Open gap**: the K8s `metadata.name` for `Job` objects follows the
  template
  `${ELB_BLAST_PROGRAM}-batch-${ELB_DB_LABEL}-job-${JOB_NUM}` (no
  `${BLAST_ELB_JOB_ID}` suffix), so two simultaneous `blastn` + `nr`
  submissions to the same namespace would collide on the apply call
  itself. Today this is masked by the per-cluster `acquire_submit_lock`
  (`api/tasks/blast/submit_lock.py`); the Stage 1 capacity gate ships
  with `max_slots_per_cluster=1` for the same reason
  ([docs/research/aks-capacity-gate.md §3.5](../../research/aks-capacity-gate.md)).
  Phase 2 (`BLAST_GATE_MAX_SLOTS_PER_CLUSTER=2`) is **blocked** on an
  upstream `elastic-blast` change that adds the `elb_job_id` short hash
  to the K8s metadata.name; tracked as a Phase 2 entry-criterion in the
  design doc rollout matrix.

## Validation

```
uv run pytest -q api/tests/test_terminal_exec_workdir.py
```
