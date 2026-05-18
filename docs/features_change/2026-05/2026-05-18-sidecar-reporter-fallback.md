# Sidecar Reporter Fallback

## Motivation

The deployed Container App revision can run the `frontend` and `terminal` containers successfully while the dashboard still marks those sidecars as unavailable. Their standalone metrics reporters exited when `/sys/fs/cgroup/cpu.stat` was unavailable, so the API stopped receiving `sidecar:metrics:<name>` heartbeats in Redis.

## User-facing change

The Sidecars card now receives heartbeat snapshots from `frontend` and `terminal` even when cgroup v2 files are not mounted. In that environment the reporters publish procfs-based self-process metrics with `source: "procfs"` instead of exiting.

## API / IaC diff summary

- Updated `web/cgroup_reporter.py` and `terminal/cgroup_reporter.py` to fall back from cgroup v2 files to `/proc/self/stat` and `/proc/self/status`.
- No API route, storage, RBAC, or Bicep changes.

## Validation evidence

- `python3 -m py_compile web/cgroup_reporter.py terminal/cgroup_reporter.py`
- `uv run ruff check api web/cgroup_reporter.py terminal/cgroup_reporter.py`
- `uv run pytest -q api/tests` → 604 passed.
- Local fallback smoke: forced `CGROUP_ROOT` to a missing path and verified both scripts select `source: "procfs"`.
- ACR build and Container App rollout:
	- `elb-frontend:20260518020906-reporterfix` built in 91 seconds.
	- `elb-terminal:20260518020906-reporterfix` built in 337 seconds.
	- `ca-elb-control--0000044` became `RunningAtMaxScale` / `Healthy`.
	- All six containers reported `Running` and `ready=true`.
- Internal Redis heartbeat check from the `redis` sidecar returned both `sidecar:metrics:frontend` and `sidecar:metrics:terminal` payloads with `source: "procfs"`.
- Public endpoints:
	- `/` → HTTP 200.
	- `/api/health` → HTTP 200, revision `ca-elb-control--0000044`.
	- `/api/terminal/health` → HTTP 200, `status: "ok"`.
- ACR was restored to `publicNetworkAccess: Disabled` and `networkRuleSet.defaultAction: Deny` after the build.