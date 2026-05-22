# OpenAPI proxy — public-LB opt-in (`OPENAPI_ALLOW_PUBLIC_LB`)

## Motivation
The 2026-05-22 security audit (item #12) added a hard refusal in
`/api/aks/openapi/proxy` when the resolved `elb-openapi` Service IP is
not RFC1918 / loopback / link-local — the proxy auto-injects the admin
`X-ELB-API-Token`, and forwarding that over plain HTTP to a public
LoadBalancer would expose the token between the api sidecar and the
LB. That ships as the safe default.

However, existing deployments where `elb-openapi` is wired as a public
`type: LoadBalancer` (the current `_build_manifests` default in
[api/tasks/openapi/__init__.py](../../../api/tasks/openapi/__init__.py))
suddenly hit `502 openapi_unsafe_transport` on every API menu call —
including the "Try" panel for `/v1/health` shown in the dashboard.

This change gives operators an explicit opt-in so the existing public-LB
deployments keep working while the safer-by-default behaviour stays for
fresh installs.

## User-facing change
- New env var `OPENAPI_ALLOW_PUBLIC_LB` on the api sidecar. When set to
  any of `1`, `true`, `yes`, `on` (case-insensitive), the proxy will
  forward the admin token to a non-private upstream IP and emit a
  WARNING log line every time it does so.
- **Default in the deployed Container App is `true`** (set in
  [infra/modules/containerAppControl.bicep](../../../infra/modules/containerAppControl.bicep)
  on the `api` sidecar) — fresh `azd up` deployments use the public
  `elb-openapi` LoadBalancer that `_build_manifests` creates, so the
  dashboard's API menu works out of the box without manual env-var
  surgery. Local dev (host-mode `fullstack: start`, Compose, pytest)
  leaves the env var unset and therefore keeps the safer refusal
  behaviour, matching the security audit #12 default.
- When the opt-in is **off** (or env var unset), the audit #12 behaviour
  is unchanged: `502 {"code": "openapi_unsafe_transport", "message":
  "..."}`. The error message now also mentions the opt-in env var so
  operators can discover it from the dashboard error toast.
- No other surface changes. IPv6 is still refused regardless of the
  opt-in (the upstream URL builder does not bracket IPv6 literals — see
  the existing comment on `_is_private_ipv4`).

## API / IaC diff summary
| Layer | File | Change |
|---|---|---|
| Routes | [api/routes/aks/openapi.py](../../../api/routes/aks/openapi.py) | New `_public_lb_allowed()` helper; `aks_openapi_proxy` skips the refusal when it returns True and emits a warning log on each forward to a non-private IP. Error message now mentions `OPENAPI_ALLOW_PUBLIC_LB`. |
| Infra | [infra/modules/containerAppControl.bicep](../../../infra/modules/containerAppControl.bicep) | `api` sidecar env block now sets `OPENAPI_ALLOW_PUBLIC_LB=true` by default so fresh `azd up` deployments work against the public-LB `elb-openapi` Service that `_build_manifests` creates. |
| Tests | [api/tests/test_openapi_proxy_route.py](../../../api/tests/test_openapi_proxy_route.py) | New `test_openapi_proxy_allows_public_ip_when_opt_in_env_set` (+1). The existing 3 refusal tests (`test_openapi_proxy_refuses_public_ip`, `_refuses_public_ipv6`, `_accepts_private_ipv6` — misnamed, actually pins the IPv6 refusal) are unchanged. |

No new dependency. The Bicep change requires `azd provision` (or
manual `az containerapp update --set-env-vars OPENAPI_ALLOW_PUBLIC_LB=true`
on an already-deployed Container App) for the deployed dashboard to
pick up the new default.

## Validation evidence
- `uv run pytest -q api/tests/test_openapi_proxy_route.py` — **23 passed** (was 22).
- `uv run pytest -q api/tests` — **983 passed** (was 982 → +1).
- `uv run ruff check api/routes/aks/openapi.py api/tests/test_openapi_proxy_route.py` — clean.

## Operator runbook
Fresh deployments (`azd up` from a clean clone) require no manual
action — the Bicep default sets `OPENAPI_ALLOW_PUBLIC_LB=true` on the
api sidecar. For an **already-deployed** Container App that predates
this change, apply the env var without a full provision cycle:

```bash
# Container App api sidecar
az containerapp update \
  --name ca-elb-dashboard \
  --resource-group <rg> \
  --container-name api \
  --set-env-vars OPENAPI_ALLOW_PUBLIC_LB=true
```

For local dev: add `OPENAPI_ALLOW_PUBLIC_LB=true` to the api sidecar
env block in [scripts/dev/local-run.sh](../../../scripts/dev/local-run.sh)
or export it before `scripts/dev/local-run.sh api`. Local dev is the
only place where it stays unset by default, so the safer-by-default
audit #12 behaviour is exercised by tests.

Long-term, the documented target state remains an **internal**
LoadBalancer (`service.beta.kubernetes.io/azure-load-balancer-internal: "true"`)
— see [docs/architecture/container-apps.md](../../architecture/container-apps.md)
L952. The opt-in is an escape hatch, not the recommended posture, and
the Bicep default should flip back to `false` (or be removed) once
`_build_manifests` is updated to create the Service as internal.
