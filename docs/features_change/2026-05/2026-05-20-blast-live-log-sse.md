# BLAST Live Log SSE

## Motivation

Run details showed submit output through state snapshots and polling. Fast K8s pods such as `init-ssd-*` could finish before the UI fetched their logs, and completed jobs could still show submit output line-by-line instead of a true live stream.

## User-Facing Change

- Run details now opens a ticketed Server-Sent Events stream while a BLAST job is active.
- Live terminal submit lines and Kubernetes pod/container logs are appended to the matching execution step.
- Existing execution-step polling remains the fallback and post-completion snapshot path.

## API / IaC Diff Summary

- Added `api.services.job_log_events` as the common live log event layer:
  - terminal/Celery producers publish sanitised events to a capped Redis Stream;
  - Kubernetes log targets are discovered by job ownership, ElasticBLAST job suffix, labels, and `BLAST_ELB_JOB_ID` env values;
  - pod logs are followed directly through the Kubernetes API with `follow=true`, `timestamps=true`, `tailLines`, and explicit container names.
- Split live log Python responsibilities into SRP-focused modules:
  - `api.services.job_logs.event_bus` owns Redis Stream publish/read;
  - `api.services.job_logs.k8s` owns Kubernetes target discovery and pod-log follow;
  - `api.services.job_log_events` remains a compatibility facade with explicit `__all__` re-exports.
- Added `/api/blast/logs/{job_id}/ticket` and `/api/blast/logs/{job_id}/events`.
  - The ticket endpoint validates the MSAL/dev-bypass caller and binds the ticket to one job and owner.
  - The SSE endpoint fans in Redis live events plus direct K8s pod log follow frames.
- `elastic-blast submit` streaming now publishes each stdout/stderr line into the common live log event stream in addition to existing artifact chunks.
- Frontend Run details now requests a log stream ticket and appends phase-matched live log lines to the open execution step.
- The HTTP inspector excludes `/api/blast/logs` because SSE responses are long-lived and should not be body-buffered.
- No IaC resource shape changes.

## Hardening Notes

- Browser EventSource cannot attach bearer headers, so logs use the existing ticket pattern instead of accepting raw job ids over an unauthenticated stream.
- Tickets are single-use, short-lived, owner-bound, and job-bound.
- Kubernetes log targets are discovered server-side only; the browser cannot request arbitrary pod/container logs through this route.
- Kubernetes log lines are read through the Kubernetes API, not `kubectl logs` shell-out, so cancellation, container selection, and future backpressure controls stay in process.
- Redis Stream history is capped to prevent unbounded broker memory growth.
- Client log buffers are capped to 500 events and per-step display is capped to the latest 80 lines.
- Existing `/execution-steps` polling remains the fallback and post-completion path.

## Validation Evidence

- Live log service tests: `uv run pytest -q api/tests/test_job_log_event_bus.py api/tests/test_job_log_k8s.py api/tests/test_blast_log_routes.py`.
- Route hardening/auth smoke: `uv run pytest -q api/tests/test_job_log_event_bus.py api/tests/test_job_log_k8s.py api/tests/test_blast_log_routes.py api/tests/test_smoke.py::test_auth_required_endpoints_reject_anonymous`.
- Focused backend regression: `uv run pytest -q api/tests/test_job_log_event_bus.py api/tests/test_job_log_k8s.py api/tests/test_blast_log_routes.py api/tests/test_local_to_blast_job.py api/tests/test_blast_tasks.py::test_merge_progress_payload_completes_previous_running_steps api/tests/test_smoke.py::test_auth_required_endpoints_reject_anonymous` -> 40 passed.
- Backend lint: `uv run ruff check api/services/job_log_events.py api/services/job_logs api/routes/blast/logs.py api/tasks/blast/__init__.py api/tests/test_job_log_event_bus.py api/tests/test_job_log_k8s.py api/tests/test_blast_log_routes.py api/tests/test_smoke.py` -> passed.
- Full backend regression after SRP split: `uv run pytest -q api/tests` -> 786 passed.
- Frontend build: `cd web && npm run build` -> passed. Vite reported the existing large chunk warning only.