# Public HTTPS enable: lift terminal exec server body cap to 8 MiB

## Motivation

Clicking **Enable** on Settings → Public HTTPS failed instantly with the SPA
toast:

```
exec server returned 413: {"error": "body too large (max 65536)"}
```

`setup_openapi_public_https` step 1 (`install_ingress_nginx`) pipes the
upstream [`ingress-nginx`](https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.11.3/deploy/static/provider/cloud/deploy.yaml)
install manifest through `kubectl apply -f -` via the terminal sidecar's
exec server. The JSON-encoded `stdin` field exceeds the previous 64 KiB
`MAX_BODY_BYTES` cap (raw manifest is ~30 KiB; cert-manager in step 4 is
~1.7 MiB), so the request was rejected before `kubectl` ever ran.

## User-facing change

* Settings → Public HTTPS → **Enable** now proceeds past step 1 on AKS
  clusters that previously could only fail with the 413.
* No new buttons, no new env vars on the api/worker side — the only
  knob is the same `EXEC_MAX_BODY_BYTES` env var the exec server has
  always honoured.

## API / IaC diff summary

* `terminal/exec_server.py` — `MAX_BODY_BYTES` default raised
  `64 KiB → 8 MiB` (matches the existing `EXEC_RUN_MAX_OUTPUT_BYTES`
  ceiling; endpoint stays loopback-only + `X-Exec-Token`-authenticated).
* `infra/modules/containerAppControl.bicep` + regenerated
  `infra/main.json` — terminal sidecar now sets
  `EXEC_MAX_BODY_BYTES=8388608` explicitly so the knob is visible in the
  Container App manifest and can be rotated by Bicep alone (no image
  rebuild) in the future.
* `scripts/dev/docker-compose.full.yml` — terminal service sets the
  same value so the local 6-sidecar stack matches production.
* No SPA, route, task, or service-wrapper changes.

## Validation

* `uv run pytest -q api/tests/test_terminal_exec.py api/tests/test_openapi_public_https.py`
  → 35 passed (boots `terminal/exec_server.py` in a background thread
  and exercises buffered + streaming exec paths, plus the full
  `setup_openapi_public_https` 9-step pipeline with mocked kubectl).
* Manual: a `kubectl apply -f -` of the ingress-nginx install manifest
  (the previously failing payload) is now under the 8 MiB cap by ~250×.

## Rollback

Set `EXEC_MAX_BODY_BYTES` back to `65536` on the terminal sidecar in
Bicep and re-deploy. No data migration; the cap is enforced per request.
