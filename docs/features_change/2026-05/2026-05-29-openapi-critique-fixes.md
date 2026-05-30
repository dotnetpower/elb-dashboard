# 2026-05-29 — openapi /v1/ready critique fixes

## Motivation

The 2026-05-29 `/v1/ready` hardening landed with 1859 LOC backend + 421 LOC SPA
and 1859 + 10 sibling tests green, but a follow-up self-critique surfaced 10
gaps. This change closes the top-severity ones in a single coordinated diff
across the dashboard repo and the sibling `elastic-blast-azure/docker-openapi`
runtime so the dashboard's pre-flight UX, in-process safety, and operator
remediation hints all stay aligned.

## User-facing change

* **SPA pre-flight 409 surfaces remediation hints.** When `POST /api/blast/jobs`
  is blocked by a pre-flight gate (`detail.code == "blocked_by_preflight"`), the
  SPA now renders the per-gate `message` + `action` strings from the backend
  envelope instead of falling through to the generic 4xx fallback.
  ([web/src/api/client.ts](../../../web/src/api/client.ts))
* **PLS transition banner on the API Reference page.** When the deploy
  environment has `OPENAPI_PLS_ENABLED=1` but the live `elb-openapi` Service is
  missing the `azure-pls-create` annotation, the page now shows a yellow banner
  explaining that the next deploy must re-create the Service and the operator
  needs `OPENAPI_PLS_CONFIRM_RECREATE=1`. Hidden when the probe is unavailable
  or already in lock-step.
  ([web/src/pages/apiReference/PlsTransitionBanner.tsx](../../../web/src/pages/apiReference/PlsTransitionBanner.tsx))
* **Sibling /v1/ready hardening (`docker-openapi` 3.7.2).**
  * Per-IP anonymous bucket — one noisy laptop can no longer DoS the shared
    `anonymous` quota for every other unauthenticated caller.
  * Empty rate buckets garbage-collected — long-running pods serving many
    distinct tokens / IPs no longer accumulate unbounded SHA-256 keys.
  * Optional stricter autoscaler probe via `ELB_OPENAPI_WORKLOAD_POOL_NAME` —
    when set, the autoscaler ConfigMap *body* must mention that pool, so a
    multi-pool cluster with autoscaler on a non-workload pool no longer
    silently degrades a real outage into `autoscaler_pending`.

## API / IaC diff summary

| Surface | Change |
| --- | --- |
| `api/services/blast/submit_gates.py` | Public `openapi_known_upstream_codes()` + `OPENAPI_NESTED_UPSTREAM_CODES` constant. `_openapi_action_for_code` now a thin lookup into `OPENAPI_UPSTREAM_ACTIONS`. |
| `api/services/external_blast.py` | Inflight-coalesce probe (single upstream call when N callers race), structured `event=ready_probe_cached` log for cache hits with `cached_age_seconds`, normalised cache key (lowercased base + full sha256 hex). |
| `api/services/image_tags.py` | `elb-openapi` pin `4.15` → `4.16` (tracks sibling 3.7.2). |
| `api/tests/test_openapi_upstream_codes_contract.py` | New contract test asserting dashboard ↔ SPA hint-table parity. |
| `web/src/api/client.ts` | New `blockedByPreflightMessage()` + `PreflightBlockingGate` type for the 409 envelope. |
| `web/src/api/aks.ts` | New `OpenApiPlsStatus` type + `openApiPls()` method. |
| `web/src/pages/apiReference/PlsTransitionBanner.tsx` (new) | Banner component wired into `ApiReference.tsx` between `OpenApiDeployPanel` and `ApiTokenPanel`. |
| sibling `docker-openapi/app/main.py` | `VERSION = 3.7.2`; per-IP anonymous bucket; empty-bucket GC; optional `ELB_OPENAPI_WORKLOAD_POOL_NAME` filter on the autoscaler probe. |
| sibling `docker-openapi/tests/test_ready.py` | +3 tests (per-IP bucket isolation, GC of empty keys, autoscaler pool-name filter pass / fail). |

No IaC changes. No new dependencies. Storage `publicNetworkAccess: Disabled`
posture untouched. ttyd loopback contract untouched.

## New env knobs

| Sidecar | Env | Default | Meaning |
| --- | --- | --- | --- |
| `terminal` / sibling `elb-openapi` | `ELB_OPENAPI_WORKLOAD_POOL_NAME` | empty | When set, the autoscaler-aware probe additionally requires the autoscaler ConfigMap body to mention this pool (case-insensitive substring against `.data.status`). |
| `api` | `OPENAPI_READY_INFLIGHT_WAIT_SECONDS` | `6.0` | How long a non-leader caller waits for the inflight leader's upstream probe before re-checking the cache. |

## Validation evidence

```
$ cd /home/moonchoi/dev/elb-dashboard
$ uv run pytest -q api/tests
1873 passed, 3 skipped in 35.66s

$ uv run ruff check api
All checks passed!

$ cd web && npm test -- --run
Test Files  54 passed (54)
     Tests  425 passed (425)

$ npm run build
✓ built in 7.43s

$ cd /home/moonchoi/dev/elb-dashboard
$ uv run python scripts/docs/check_frontmatter.py
OK — frontmatter guard checked 49 navigated pages.

$ cd /home/moonchoi/dev/elastic-blast-azure/docker-openapi
$ python -m pytest tests/test_ready.py -q
14 passed, 30 warnings in 1.19s
```

## Rollout order (added 2026-05-30 — critique #20.11)

**Cross-repo image bumps must follow this order**, otherwise the dashboard
ships a Container App template pointing at a tag that does not yet exist
in ACR and the next `azd up` / sidecar redeploy fails with
`ImagePullBackOff: manifest unknown`. This change note originally
described the dashboard pin moving `4.15 → 4.16` together with the
sibling source moving to `VERSION = 3.7.2`, but it did **not** call out
that `4.16` had never been built+pushed at the time the dashboard pin
landed — a follow-up critique (issue #20 P0 #1) flagged the gap and
the pin had to be rolled back to `4.14` on 2026-05-30 (see
[2026-05-30-openapi-pin-p0-rollback.md](2026-05-30-openapi-pin-p0-rollback.md)).

The safe order for every future bump:

1. **Commit + push the sibling change.** Update
   `~/dev/elastic-blast-azure/docker-openapi/app/main.py` `VERSION`,
   land the actual code/test changes, and push the sibling commit to
   `master`.
2. **Build the new image in ACR.** Run
   `az acr build -r acrelbdashboard3abp67bppe -t elb-openapi:<NEW_TAG> -f ~/dev/elastic-blast-azure/docker-openapi/Dockerfile ~/dev/elastic-blast-azure/docker-openapi`
   and confirm the tag shows up via
   `az acr repository show-tags --name acrelbdashboard3abp67bppe --repository elb-openapi`.
3. **Then bump the dashboard pin.** Update
   `api/services/image_tags.py::IMAGE_TAGS["elb-openapi"]` to the new
   tag, refresh the comment block that documents the version mapping,
   and write a per-bump change note recording the sibling commit SHA
   and ACR digest.
4. **Validate end-to-end.** `uv run pytest -q api/tests`, `cd web && npm
   test -- --run`, then `scripts/dev/quick-deploy.sh openapi` (or
   `apply_template`) against a dev environment and confirm the new
   ConfigMap is picked up.

The 2026-05-30 P0 rollback exists because steps 1–2 had not completed
when the dashboard pin moved in step 3. Future contributors: collapsing
the three steps into a single PR is a charter violation.
