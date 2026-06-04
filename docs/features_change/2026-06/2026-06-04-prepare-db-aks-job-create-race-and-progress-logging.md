# prepare-db AKS: cancel→re-Get Job-name race fix + pod progress logging

## Motivation

A user cancelled an in-flight `nt` prepare-db run and clicked **Get** again, then
reported "the download doesn't seem to work — there are no logs in the pod either".

Investigation produced two findings:

1. **The download was, in fact, healthy.** Live inspection of the resubmitted
   Indexed Job `prepare-db-nt-260526010501` showed all 10 shard pods `Running`,
   pod 0 actively streaming `nt_viruses.10.nsq` from NCBI through `azcopy
   copy ... --from-to=PipeBlob`, eth0 RX advancing ~16 MB/s, and 3728 `nt`
   blobs already staged. The perceived stall was **opaque logging**: the
   per-file copy runs with `--log-level=ERROR >/dev/null`, so a healthy
   multi-GB shard emits **zero** log lines for minutes between "azcopy
   installed" and the final summary. Silence looked like failure.

2. **A real latent race existed** in the cancel→resubmit path. prepare-db Jobs
   are deterministically named per `(db, source_version)`. If a cancel
   (`propagationPolicy: Background`) has not finished collecting the old Job
   when the resubmit fires, `_create_job_if_absent` previously saw the
   still-terminating Job via `GET` and reported it as a healthy `existing`
   duplicate — so the Celery task polled a dying Job and never spawned fresh
   pods. This did not fire in the reported incident (the resubmit created the
   Job cleanly), but it is a genuine defect worth closing.

## User-facing change

* **Pod logs now show progress.** `PREPARE_DB_AKS_SCRIPT` logs a throttled
  heartbeat:
  * one `[<idx>/<total>] copy <file>` line immediately before each file's
    `curl | azcopy` copy (turns opaque silence into a visibly-advancing
    counter — operators can now tell slow-but-healthy from stalled);
  * one `[<idx>/<total>] scanned; <skip> already staged, <ok> copied` line
    every 50 skipped files (so a long resume that re-checks thousands of
    already-staged blobs still looks alive while it scans).
* **Cancel→re-Get no longer collides with a terminating Job.**
  `_create_job_if_absent` now distinguishes a healthy duplicate from a Job
  carrying `metadata.deletionTimestamp`: it waits (default 60 s, polling every
  2 s) for the terminating Job to disappear, then creates the new one. If the
  old Job never clears within the deadline it returns an honest
  `{"status": "error", "terminating": True}` which the task surfaces as a
  partial "retry shortly" state (clearing `update_in_progress` so the user can
  re-Get) instead of silently polling a dead Job.

## API / IaC diff summary

* `api/services/k8s/prepare_db_jobs.py`
  * `import time`; new constants `DEFAULT_TERMINATING_WAIT_SECONDS = 60.0`,
    `DEFAULT_TERMINATING_POLL_SECONDS = 2.0`.
  * `_create_job_if_absent(..., terminating_wait_seconds, poll_interval_seconds)`
    rewritten to detect `metadata.deletionTimestamp`, wait for 404, and handle
    create-time 409 by re-evaluating. Healthy-duplicate contract
    (`status: existing`) unchanged.
  * `PREPARE_DB_AKS_SCRIPT`: added the per-file copy heartbeat and the
    throttled skip heartbeat described above. No change to the copy command
    itself (`curl | azcopy copy ... --from-to=PipeBlob`).
* No IaC change. The new script ships in the per-job ConfigMap built at submit
  time and takes effect on the **next** prepare-db job after the api/worker
  images are next deployed — no redeploy was performed for this change.

## Validation evidence

* `uv run ruff check api/services/k8s/prepare_db_jobs.py api/tests/test_prepare_db_aks_job_create.py` → All checks passed.
* `uv run pytest -q api/tests -k "prepare_db or k8s"` → **204 passed**.
* New focused suite `api/tests/test_prepare_db_aks_job_create.py` (6 cases):
  absent→created; healthy 200→existing; terminating→404→created; terminating
  never clears→error+terminating; create 409→re-eval→existing; GET 500→error.
* Live cluster evidence (moonchoi sub, `elb-cluster-02`): pod 0 of
  `prepare-db-nt-260526010501` running `curl nt_viruses.10.nsq` +
  `azcopy copy PipeBlob`; eth0 RX +79,273,930 bytes over 5 s; 10/10 pods
  `Running`; 3728 `nt` blobs staged — confirming the download was never stuck.
