# 2026-05-15 — Production hardening: MI sub-scope Reader + ARM diagnostics

## Motivation

Same-day investigation of the local "구독을 못 찾는데?" failure
(see [2026-05-15-dev-compose-az-cli-mount.md](2026-05-15-dev-compose-az-cli-mount.md))
revealed that the production Bicep had the **mirror image** of the bug:

* The shared UAMI is correctly attached to the Container App and
  `AZURE_CLIENT_ID` is correctly injected on every sidecar — so
  `subscriptions.list()` works because every per-resource role
  assignment (ACR/KV/Storage) also surfaces its enclosing subscription.
* But the per-resource scope is **not** enough for the SPA's discovery
  wizard, which calls `resource_groups.list()` and
  `storage_accounts.list_by_resource_group()` /
  `registries.list_by_resource_group()` /
  `virtual_machines.list()`. Those need at least `Reader` at
  subscription scope.
* Bicep granted no subscription-scope role. `docs/auth.md` instructed
  the operator to run a one-line `az role assignment create` after
  `azd up` — which is exactly the kind of step that gets skipped and
  produces an identical "succeed-but-empty" failure to the local one.
* On top of that, every `/api/arm/list_*` route swallowed exceptions
  and logged only `type(exc).__name__`, leaving the operator with zero
  signal about whether the empty list meant "RBAC missing", "private
  network blocked", or "subscription truly is empty".

## User-facing change

* `azd provision` now grants the shared UAMI `Reader` at subscription
  scope by default. The wizard's discovery flow works on first deploy
  without manual `az role assignment` follow-up.
* New unauthenticated probe `GET /api/health/azure-discovery` walks
  the credential → `subscriptions.list()` → `resource_groups.list()`
  chain and returns the first failing step with a remediation hint.
  Operators can curl it once after deploy to confirm RBAC.
* Failed `/api/arm/list_*` calls now log the exception message
  (sanitised) plus full traceback so the prod logs surface the real
  cause instead of just a class name.

## API / IaC diff summary

* **`infra/modules/subscriptionRoles.bicep`** (new) — sub-scope
  `Microsoft.Authorization/roleAssignments` granting the UAMI the
  built-in `Reader` role. Targets subscription scope.
* **`infra/main.bicep`** — new param `assignSubscriptionReader bool =
  true` and conditional module include for the new module. Default-on
  so behaviour is correct out of the box; can be turned off in
  restricted tenants where the deployer lacks
  `User Access Administrator` (the failure mode for that case is
  documented in the new post-deploy checklist).
* **`api/routes/arm.py`** — `list_subscriptions`,
  `list_resource_groups`, `list_storage_accounts`, `list_acrs`,
  `list_vms` log `type(exc).__name__: sanitised message` with
  `exc_info=True` (the silent fallback to `[]` is preserved so the SPA
  still degrades gracefully).
* **`api/routes/health.py`** — new `GET /api/health/azure-discovery`
  endpoint: unauth, read-only, hard-capped to 5 subs and 1 RG list,
  surfaces a `hint` for whichever step fails. Comment in the endpoint
  explicitly forbids polling it from a dashboard.
* **`docs/container-apps-migration.md`** — appended a
  "Post-deploy Smoke Checklist (RBAC + discovery)" section that walks
  through MI attach / `AZURE_CLIENT_ID` / probe / sub-scope Reader /
  log inspection. Cross-links the local-compose mirror change.

## Validation evidence

```
$ az bicep build --file infra/main.bicep --stdout >/dev/null && echo OK
OK

$ uv run pytest -q api/tests
120 passed in 11.61s

$ docker compose ... restart api
$ curl -fsS http://127.0.0.1:18080/api/health/azure-discovery | jq
{
  "credential":          { "status": "ok", "type": "DefaultAzureCredential" },
  "subscriptions_list":  { "status": "ok", "count_capped_at_5": 1, "samples":[…] },
  "resource_groups_list":{ "status": "ok", "subscription_id":"b0523…", "count": 36 },
  "hint": null
}
```

## Trade-offs / safety notes

* The new `Reader` role is read-only at subscription scope. No
  control-plane writes are granted at sub scope; data-plane writes
  remain pinned to the per-resource `Storage Blob Data Contributor`,
  `AcrPush`, etc. assignments.
* Default-on (`assignSubscriptionReader=true`) requires the deployer
  to have `User Access Administrator`. If the role assignment fails
  during `azd provision`, the operator can flip the param to `false`
  and run the equivalent CLI by hand — the new post-deploy checklist
  documents this.
* `/api/health/azure-discovery` makes two real ARM calls per request.
  It is unauth like the rest of `/api/health/*` and the response
  contains only counts + sanitised error strings, never tokens.

## Follow-up hardening (same day, after critique)

The first cut of `/api/health/azure-discovery` was unauthenticated and
echoed raw `subscription_id` + `display_name` values. That violates
the §12 "never echo subscription IDs" rule — anyone with ingress
reachability could harvest tenant topology. Tightened in three steps:

1. **Auth gate.** The endpoint now `Depends(require_caller)` (MSAL
   bearer required, same as `/api/me`). The other `/api/health/*`
   probes stay unauth because they only return liveness booleans.
2. **Sanitise everything that touches a sub.** All `subscription_id`
   values pass through `services.sanitise.sanitise()` (first 8 chars
   of each GUID, then `…`). `display_name` is dropped from the
   response entirely because `sanitise()` does not mask org/billing
   strings and the diagnostic does not need them.
3. **Tests.**
   - `test_auth_required_endpoints_reject_anonymous` now covers the
     new endpoint (401 without bearer).
   - `test_azure_discovery_probe_credential_failure` proves the
     credential-failure branch short-circuits with a hint.
   - `test_azure_discovery_probe_sanitises_subscription_ids` mocks a
     successful list and asserts the raw GUID + display name never
     appear in the response body.

`pytest -q api/tests` → 123 passed (was 120; +3 new).

