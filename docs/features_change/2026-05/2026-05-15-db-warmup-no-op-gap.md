# 2026-05-15 — DB Warmup is a no-op (verification-only); legacy DaemonSet path missing

## Motivation

Following the wiring fix in
[2026-05-15-warmup-endpoint-fix.md](2026-05-15-warmup-endpoint-fix.md),
clicking **Start warmup** in the AKS cluster detail modal now returns a
real Celery `AsyncResult` and the SPA observes `Completed`. Despite that,
the user reports "DB warmup이 문제가 있어" because the **actual** behaviour
on the AKS cluster is unchanged — no database is loaded onto node SSDs.

## Symptom

1. SPA → `POST /api/warmup/start` with `{"db": "blast-db/core_nt", "db_display_name": "core_nt", ...}`.
2. Backend returns `200` in ~150 ms with `{instance_id, db: "core_nt", status: "queued"}`.
3. The Celery worker picks up `api.tasks.storage.warmup_database`,
   issues a `ContainerClient.list_blobs(prefix="core_nt")` style probe
   against `https://elbstg01.blob.core.windows.net/blast-db`,
   confirms the DB has files, and **returns `Completed/succeeded` in <2 s**.
4. SPA renders "Warmup (core_nt): Completed" with a green check.
5. `GET /api/monitor/aks/warmup-status` keeps reporting:

   ```json
   { "warm": true, "workspace_ready": 3, "workspace_desired": 3,
     "databases": [], "vmtouch_ready": 0, "namespaces": [] }
   ```

   `warm: true` is from the always-on `create-workspace` DaemonSet —
   it just means `/workspace` is mounted on every node. **`databases: []`**
   is the truth: no DB is actually cached on any node, so the next
   ElasticBLAST submit will pay the full cold-start price.

## Root cause

`api/tasks/storage.py::warmup_database` (current `HEAD~1` version was
already a thin wrapper over `terminal_exec → elastic-blast get-blastdb`;
the unstaged diff that the parallel session is currently sitting on
intentionally **downgrades** it further to a pure verification check):

```python
databases = list_databases(get_credential(), storage_account)
match = next((db for db in databases if db.get("name") == database_name), None)
if match and int(match.get("file_count") or 0) > 0:
    return { "status": "completed",
             "output": "Database is prepared in workload storage." }
return { "status": "failed",
         "error": f"database {database_name!r} is not prepared in workload storage" }
```

That check answers the question "is this DB staged in
`https://<account>.blob.core.windows.net/blast-db/`?" — it does **not**
warm node SSDs. The label "warmup" is misleading.

The real warmup path that the dashboard's UI was originally designed
against still lives in the retired Functions tree:

* **Orchestrator** — [legacy/functionapp/orchestrators/warmup_db.py](../../../legacy/functionapp/orchestrators/warmup_db.py) `:1-200`
  6-phase flow: `enabling_storage → configuring → roles → warming_up
  → polling → disable_storage`. Polls up to
  `WARMUP_POLL_MAX_ATTEMPTS=480 × 15 s = 120 min`
  (sized for `core_nt` ≈ 283 GB).
* **Activity** — [legacy/functionapp/activities/blast.py](../../../legacy/functionapp/activities/blast.py) `:1170-1310`
  `activity_k8s_warmup_db` creates a Kubernetes **DaemonSet**
  `warmup-{safe_db}` (label `app=db-warmup,db={safe}`)
  with an `initContainer` that runs:

  ```bash
  export AZCOPY_AUTO_LOGIN_TYPE=MSI
  for i in 1..6; do
    azcopy cp "$DB_URL/*" "$TMP_DIR/" --recursive --log-level=WARNING && break
    sleep 30   # tolerate kubelet RBAC propagation
  done
  find "$TMP_DIR" -name "${db_name}*" -exec mv {} "$DB_DIR/" \;
  blastdbcmd -db "$db_name" -info -json > "$db_name.njs"
  ```

  Volume `hostPath /workspace` (DirectoryOrCreate) → `/workspace`,
  `requests cpu=1 memory=1Gi`, `limits memory=4Gi`,
  `pause` container is `registry.k8s.io/pause:3.9`.
  `activity_k8s_check_warmup_db` polls pods via
  `labelSelector=app=db-warmup,db={safe}` and surfaces init-container
  errors + logs.

The SPA's `WarmupSection` (see [web/src/components/WarmupSection.tsx](../../../web/src/components/WarmupSection.tsx))
still renders phase strings (`enabling_storage`, `warming_up`,
`Loading DB to nodes... (X/Y)`) that imply this DaemonSet is being
created — but the new Celery task never creates it.

## Why this PR does NOT include the fix

`api/tasks/storage.py`, `api/routes/stubs.py`, `api/main.py`, and
`api/celery_app.py` are all currently `M` in another active editor
session's working copy:

```
$ git status --short api/
 M api/celery_app.py
 M api/main.py
 M api/routes/stubs.py
 M api/tasks/acr.py
 M api/tasks/azure.py
 M api/tasks/blast.py
 M api/tasks/storage.py
?? api/tasks/openapi.py
```

That session's unstaged diff already touches the warmup region of both
`api/tasks/storage.py` (the verification logic shown above) and
`api/routes/stubs.py` (the wiring fix in
[2026-05-15-warmup-endpoint-fix.md](2026-05-15-warmup-endpoint-fix.md)).
Porting the legacy DaemonSet flow now would force a merge in three
files that are mid-edit, and the verification-only behaviour appears
to be intentional in their direction (the old `terminal_exec → elastic-blast
get-blastdb` call was deliberately removed). Coordination is required
before code lands.

## Recommended fix path (for the next session that owns these files)

1. Restore the real warmup path. Either:
   * **Port the legacy DaemonSet flow** into a new
     `api/services/k8s_warmup.py` (uses the existing
     `_get_k8s_session(...)` helper from `api/services/k8s_monitoring.py`
     for the cluster API; never call `ManagedClusters.begin_run_command`
     per AGENTS.md tripwire #9), then have `warmup_database` call into
     it after the storage verification step succeeds; **or**
   * **Re-introduce the `terminal_exec → elastic-blast get-blastdb`
     fallback** the previous version had, but route it through the
     `terminal` sidecar (`api/services/terminal_exec.py`) so it does
     not depend on the local-dev shell having the BLAST+ toolchain.
2. Drop the `_update_state(job_id, "downloading", ...)` lie — either
   actually download something, or relabel the phase to
   `"verifying"` so the SPA does not promise work that is not happening.
3. Make `WarmupSection.tsx` honest about the verification-only path
   while the real warmup is being implemented:
   * Rename the button to **"Verify DB staged"** (or similar) when the
     backend is in verification-only mode.
   * Hide the `Loading DB to nodes... (X/Y)` substring until
     `custom_status.steps.warming_up` actually carries `ready`/`total`.
4. Ensure `_update_state` is no-op-safe when `AZURE_TABLE_ENDPOINT`
   is unset (already best-effort, but each call logs a `WARNING` —
   demote to `DEBUG` in local-dev so the log isn't spammed for every
   warmup attempt; the parallel session's diff already wraps the
   call in `try/except`, so this is just log-noise).

## Side observation — single-worker uvicorn wedged

While reproducing the warmup behaviour on the local dev stack
(`uvicorn api.main:app --reload`, single worker), the api became
unresponsive: `/api/health` and `/api/monitor/aks/warmup-status`
both timed out at 60 s, with `ss -tn 'sport = :8080'` reporting
`102` open connections piling up. The reloader process (`663406`)
was healthy; the spawned worker (`901579`) had 6 threads, 4 stuck on
`futex_wait`. `SIGTERM` did not unwedge it; `SIGKILL` followed by an
automatic respawn restored the api, after which everything recovered
cleanly (including the warmup status query).

This is almost certainly a side effect of the SPA's per-card 30 s
TanStack Query polls stacking up against the synchronous K8s API
calls in `k8s_warmup_status` and the Storage SDK calls in
`warmup_database` running in the threadpool while a slow request is
pending. Worth a follow-up:

* Wrap `requests.get(...)` calls in `k8s_monitoring.py` with stricter
  timeouts (already 10 s per call, but five sequential calls × 10 s
  + threadpool contention can stall an event loop) and consider a
  shorter shared timeout budget.
* Bump the local-dev uvicorn to `--workers 2` or pin
  `--limit-concurrency` so a stuck request does not block all others.

That observation is logged here for the next maintainer; this PR does
not change uvicorn invocation.

## API/IaC diff summary

**No code changes.** Documentation only.

## Validation evidence

```
# 1) Warmup task: returns succeeded immediately even though no DB is loaded on nodes
$ curl -s -X POST -H 'Authorization: Bearer __dev_bypass__' \
    -H 'Content-Type: application/json' \
    -d '{"subscription_id":"...","resource_group":"rg-elb-01",
         "storage_account":"elbstg01",
         "db":"blast-db/core_nt","db_display_name":"core_nt",
         "aks_cluster_name":"elb-cluster"}' \
    "http://127.0.0.1:8080/api/warmup/start"
{"id":"...","instance_id":"a8aa..","task_id":"a8aa..",
 "db":"core_nt","statusQueryGetUri":"/api/tasks/a8aa..","status":"queued"}

$ curl -s -H 'Authorization: Bearer __dev_bypass__' \
    "http://127.0.0.1:8080/api/warmup/a8aa../status"
{"instance_id":"a8aa..","runtime_status":"Completed",
 "custom_status":{"phase":"completed","db":"core_nt"},
 "output":{"status":"succeeded","db":"core_nt"}}

# 2) Cluster reports the DB is NOT actually warm on nodes
$ curl -s -H 'Authorization: Bearer __dev_bypass__' \
    "http://127.0.0.1:8080/api/monitor/aks/warmup-status?...&cluster_name=elb-cluster"
{"warm":true,"workspace_ready":3,"workspace_desired":3,
 "databases":[], "vmtouch_ready":0, "namespaces":[]}
```

`databases: []` is the smoking gun: no `db-warmup` DaemonSet exists
on the cluster, so the verification-only "succeeded" claim is
operationally meaningless.

## Cross-repo consistency

None. The sibling [`elastic-blast-azure`](https://github.com/dotnetpower/elastic-blast-azure)
repo already handles real warmup via the same DaemonSet pattern
during `elastic-blast submit`; the dashboard's separate "warmup
button" is purely an optimization to pre-cache the DB before
submitting. Restoring it requires only control-plane code, not a
sibling change.
