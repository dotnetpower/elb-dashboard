# Service Reliability Hardening

## Motivation

Monitoring and status endpoints are polled frequently by the dashboard. A slow Durable backend or transient Azure SDK failure should return bounded, sanitized responses instead of hanging a worker or leaking raw exception text.

## Reliability critique

- Several status endpoints previously waited on Durable status reads without an explicit request-level timeout.
- Monitoring routes mixed raw Azure SDK failures with user-facing API responses, which made transient platform failures look like generic application crashes.
- Direct Kubernetes polling endpoints accepted resource names without the same validation discipline used by ARM-backed endpoints.
- Storage public-access windows accepted unbounded TTL input, which could leave a risky network posture active longer than intended.

## User-facing change

- Durable status polling endpoints now return `504` when status lookup exceeds the bounded wait.
- Monitoring endpoints for AKS, Storage, ACR, Remote Terminal, and storage public-access toggling now validate resource names before making Azure SDK calls.
- Transient Azure SDK transport/server failures now return sanitized `503` responses; missing resources return `404`.
- Storage public-access windows now reject invalid TTL values and cap the accepted range to 30-1800 seconds.

## API/IaC diff summary

- Added shared Azure SDK error mapping in `_http_utils.py`.
- Added timeout wrappers around Durable status reads in BLAST, warmup, terminal, and job detail routes.
- Hardened monitoring route validation and exception handling, including direct Kubernetes API polling endpoints.
- No IaC changes.

## Validation evidence

- `pytest -q api/tests/test_http_utils_hardening.py api/tests/test_blast_jobs_hardening.py`
- `pytest -q api/tests/test_monitor_hardening.py`
- `pytest -q`
- `ruff check api/_http_utils.py api/routes/blast.py api/routes/terminal.py api/routes/monitor.py api/routes/blast_jobs.py api/tests/test_http_utils_hardening.py api/tests/test_monitor_hardening.py`
- `python -m compileall -q api`
- `func start --python --debug-port 9091` was not run because Azure Functions Core Tools is not installed in the local environment.