# WAF six-stage hardening pass

## Motivation

The dashboard already had two performance-focused hardening batches in flight.
This pass reviewed the same working tree against a six-stage
[Well-Architected Framework](https://learn.microsoft.com/azure/well-architected/)
lens: reliability, security, cost optimization, operational excellence,
performance efficiency, and sustainability/efficiency. The goal was to improve
resilience and operator safety without changing user-facing features, API
schemas, or deployment topology.

## User-facing change

No feature behaviour changes. Misconfigured environments now fail earlier with
clearer errors, high-volume buffers have stronger caps, and operational logs are
more useful when shutdown or cache paths misbehave.

## Six-stage critique and hardening summary

1. **Reliability**
   * Capped Redis wait attempts so extreme `REDIS_WAIT_TIMEOUT` values cannot
     produce unbounded startup log spam.
   * Added tunable Storage health-probe cache TTLs and explicit cache-reset
     logging.
   * Added Celery timeout ordering validation so hard timeouts cannot fire
     before soft timeouts.
   * Added a lower-bound guard for NCBI preview HTTP timeout.

2. **Security**
   * Added a 100 MiB startup guard for `MAX_REQUEST_BODY_BYTES`.
   * Added a lower-bound guard for `OPENAPI_RATE_LIMIT_MAX_KEYS`.
   * Added an upper-bound guard for terminal `EXEC_TOKEN` size.
   * Bounded the JWKS cache tenant count and logged single-flight wait expiry.

3. **Cost Optimization**
   * Added a Celery result-backend TTL upper bound to protect Redis memory.
   * Documented Storage artifact snapshot byte caps as cost controls.
   * Preserved the previously added Storage/result listing caps and Docker build
     context exclusions.

4. **Operational Excellence**
   * Added stack traces to app shutdown debug/warning paths.
   * Improved `postprovision.sh` required-env failure guidance.
   * Kept request-detail capture opt-in and documented correlation behaviour in
     the middleware header.

5. **Performance Efficiency**
   * Made BLAST log SSE queue size tunable.
   * Made Storage probe TTLs tunable for probe amplification control.
   * Bounded UI animation event retention with a hard cap.

6. **Sustainability / Efficiency**
   * Expanded terminal `.dockerignore` to shrink build contexts.
   * Kept terminal toolchain versions pinned and included in the base-image
     content hash so rebuilds happen only when inputs change.

## Concrete improvement inventory (20)

1. `REDIS_WAIT_MAX_ATTEMPTS` bounds Redis startup wait attempts.
2. Redis startup fatal logs now include the number of attempts.
3. Storage probe OK TTL is configurable by environment.
4. Storage probe degraded TTL is configurable by environment.
5. Storage probe cache resets emit a log line.
6. Celery soft/hard task timeout ordering is validated at startup.
7. NCBI preview HTTP timeout has a safe lower bound.
8. Request body hard cap has a 100 MiB startup guard.
9. OpenAPI rate-limit key capacity has a safe lower bound.
10. Terminal exec shared token has a maximum length guard.
11. JWKS tenant cache has a maximum size.
12. JWKS single-flight wait expiry is logged.
13. Celery result-backend TTL has a 2-hour upper bound.
14. UI event retention has a hard cap even if env is oversized.
15. Job artifact byte caps are documented as Storage cost controls.
16. App shutdown paths include stack traces in debug/warning logs.
17. Request ID correlation contract is documented in middleware.
18. `postprovision.sh` explains how to recover missing azd env values.
19. BLAST log SSE queue size is environment-tunable with a safe floor.
20. Terminal build context excludes more generated/cache artifacts.

## API / IaC diff

No API schema changes and no Bicep changes in this WAF pass. Changes are limited
to runtime guardrails, environment validation, logging, Docker ignore metadata,
and documentation.

## Validation evidence

* `uv run pytest -q api/tests/test_smoke.py api/tests/test_request_metrics_detail.py api/tests/test_openapi_rate_limit.py api/tests/test_storage_data.py api/tests/test_me_route.py` — **129 passed**.
* `uv run pytest -q api/tests` — **1374 passed**.
* Focused `uv run ruff check` on touched Python files — **clean**.
* `cd web && npm run build` — **passed**.
* `az bicep build --file infra/main.bicep --stdout >/tmp/elb-main-bicep-build.json` — **passed** with only the Azure CLI Bicep version-upgrade warning.
* `bash -n scripts/dev/postprovision.sh scripts/dev/quick-deploy.sh scripts/dev/terminal-base-image.sh scripts/dev/acr-build-access.sh` — **passed**.
* `git diff --check` — **clean**.
