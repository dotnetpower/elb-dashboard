# OpenAPI token Generate — switch from strategic-merge to JSON Patch

## Motivation
After enabling the `OPENAPI_ALLOW_PUBLIC_LB` opt-in (sibling change of
2026-05-22), the dashboard's API menu reached the `elb-openapi`
LoadBalancer but the **Generate** button still returned `502 Bad
Gateway`. The proxied response body was a generic
`{"code": "openapi_token_unavailable"}` because
`_raise_openapi_route_error` swallowed every non-OpenApi exception
without logging.

After surfacing the upstream Kubernetes reason, the real error became:

```
Deployment.apps "elb-openapi" is invalid:
spec.template.spec.containers[0].env[9].valueFrom:
Invalid value: "": may not be specified when `value` is not empty
```

i.e. the running deployment's `env[9]` carries a stale `valueFrom`
field that the existing `application/strategic-merge-patch+json` body
left intact while *also* setting `value`. K8s rejects the combination.

## User-facing change
- **Generate** and **Refresh** in the API menu now patch the
  `elb-openapi` deployment via JSON Patch (RFC 6902) instead of
  strategic-merge-patch. The token env entry is replaced wholesale, so
  any stale `valueFrom` field is cleared in the same call.
- Operator no longer needs to `kubectl edit deployment/elb-openapi` to
  remove stale `valueFrom` entries before rotating the token.
- The 502 error toast now includes the upstream Kubernetes message
  (e.g. `Kubernetes returned HTTP 422 while updating the API token:
  Deployment.apps "elb-openapi" is invalid: ...`) so future patch
  failures can be diagnosed from the dashboard without log-spelunking.

## API / IaC diff summary
| Layer | File | Change |
|---|---|---|
| Services | [api/services/openapi_token.py](../../../api/services/openapi_token.py) | `_patch_deployment_token` now: (a) takes the already-fetched deployment so we know the container/env indices, (b) builds an RFC 6902 op list (`add` annotations map if missing, `add` rotated-at annotation, `replace` existing env entry by index or `add` to `/env/-`), (c) sends `application/json-patch+json`, (d) on non-2xx surfaces the K8s `message`/`reason` in the error message. |
| Tests | [api/tests/test_openapi_token.py](../../../api/tests/test_openapi_token.py) | `FakeSession.patch` accepts `Any` body; the patch test now asserts the JSON Patch shape (op list with annotations-create, rotated-at add, and env append). |

No IaC change. No new dependency.

## Validation evidence
- `uv run pytest -q api/tests/test_openapi_token.py` — **2 passed**.
- `uv run pytest -q api/tests` — **983 passed**.
- `uv run ruff check` on every touched file → clean.
- Live `curl -X POST http://127.0.0.1:8085/api/aks/openapi/token` (with
  `regenerate=true`) against the real AKS cluster → **HTTP 200**, new
  token persisted to the deployment env.
- Live `curl http://127.0.0.1:8085/api/aks/openapi/proxy?...&path=/v1/health`
  → **HTTP 200** with the elb-openapi healthy payload (was 502 before).
