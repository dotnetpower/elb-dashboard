# Public HTTPS pipeline — fix the cert-manager-webhook retry race

## Motivation

The 2026-05-27 hardening of `_wait_for_cert_manager_webhook` replaced
the original single `kubectl wait --for=condition=Available --timeout=180s`
call with a 5-retry loop around `kubectl rollout status` to absorb the
cold-cluster race where the cert-manager-webhook Deployment object did
not yet exist when the wait fired. The retry loop, however, did not
sleep between attempts.

`kubectl rollout status deployment/<name>` returns immediately with
`Error from server (NotFound)` when the Deployment object has not yet
been created server-side. The `--timeout=60s` flag only applies to
waiting for an in-progress rollout to complete, not to waiting for the
resource to exist. The result was that all 5 retries burned through in
~5 seconds of wall time instead of the intended ~5 minutes, and the
public HTTPS pipeline failed with a misleading

> kubectl rollout status cert-manager-webhook failed after 5 probes
> (~300s); last error: deployments.apps "cert-manager-webhook" not found

before cert-manager had any meaningful chance to create the webhook
Deployment. From the operator's perspective the Public HTTPS Enable
button simply did not work — the task fast-failed and the dashboard
stayed at `Enable` with a red error banner.

## User-facing change

- **The Public HTTPS Enable button now actually waits for cert-manager
  to come up on a cold cluster.** A `time.sleep(15s)` is added between
  the rollout-status retries, so the documented 5-probe budget actually
  spans ~5 minutes of wall time instead of collapsing to milliseconds.
- The accompanying error message now reports the **actual elapsed time**
  instead of an "advertised" budget that never matched reality, so
  operators can distinguish "cert-manager genuinely failed" from
  "rollout-status race burned through retries".
- The Certificate readiness wait gained a matching pre-existence probe
  (`_wait_for_certificate_object_to_exist`) that polls
  `kubectl get certificate` with `time.sleep(5s)` between attempts up
  to a ~60 s budget. cert-manager's ingress-shim creates the Certificate
  CR asynchronously after the Ingress apply, so the older
  `kubectl wait --for=condition=Ready` call could hit the same
  `NotFound`-returns-immediately behaviour on certain kubectl builds
  and fail in <1 s with a misleading "certificate not found" message.

## API / IaC diff summary

- No route changes. No SPA changes. No Bicep changes.
- `api/tasks/openapi/public_https.py`:
  - `_CERT_MANAGER_WEBHOOK_PROBE_INTERVAL_SECONDS = 15` added.
  - `_wait_for_cert_manager_webhook` sleeps between rollout-status
    retries and reports the actual elapsed time in the raised
    `RuntimeError`.
  - `_CERTIFICATE_EXISTS_PROBE_RETRIES = 12` and
    `_CERTIFICATE_EXISTS_PROBE_INTERVAL_SECONDS = 5` added.
  - New `_wait_for_certificate_object_to_exist` helper, called from
    `_wait_for_certificate_ready` before the existing `kubectl wait`.

## Validation

- `uv run pytest -q api/tests/test_openapi_public_https.py` —
  **18 passed** (was 15; +1 regression test for the missing sleep, +2
  for the new certificate pre-existence probe).
- `uv run pytest -q api/tests` — **1508 passed** (no other regressions).
- `uv run ruff check api` — All checks passed.
- Consumer search: `_wait_for_cert_manager_webhook` and
  `_wait_for_certificate_ready` are only called from
  `setup_openapi_public_https` in the same module. The new
  `_wait_for_certificate_object_to_exist` is private to the module and
  is only called from `_wait_for_certificate_ready`. The hardening is
  purely additive — the rollout-status / wait-condition contract the
  helpers expose is unchanged.

## Why this was not caught earlier

The existing
`test_wait_for_cert_manager_webhook_retries_rollout_status_then_waits`
counts the number of `kubectl rollout status` calls and asserts on
their sequence, but does not measure wall-clock time or assert on
`time.sleep` invocations. The new
`test_wait_for_cert_manager_webhook_sleeps_between_rollout_retries`
test closes that gap by recording `time.sleep` calls and asserting on
the documented interval — without that assertion, a future "make tests
faster" PR could silently re-introduce the same regression.
