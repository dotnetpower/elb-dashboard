# PR1 — self-upgrade read-only surface (2026-05-22)

## Motivation

Operators running `elb-dashboard` from a local `git clone` + `azd up` have
no out-of-band CI/CD that tells them a newer release tag is available. The
[self-upgrade design](2026-05-22-self-upgrade-design.md) ships in 4 PRs;
this PR1 is the read-only foundation: a status row, a discovery
beat-task, and three HTTP routes the SPA will eventually drive an
"upgrade available" indicator from. It performs **no build, no ARM PATCH,
no rollout** — that surface is intentionally deferred to PR2 / PR3 so the
runtime risk of this change is zero.

## User-facing change

None visible yet. Once an operator sets `UPGRADE_GIT_REMOTE` to the URL
of their git remote (`https://…/elb-dashboard.git`), the backend begins
exposing:

* `GET /api/upgrade/status` — persisted state row (defaults until the
  first check runs).
* `GET /api/upgrade/candidates` — semver tags `> running_version`
  (newest first), or `configured=false` when the env is unset.
* `POST /api/upgrade/check` — forces a discovery round; throttled at
  15 s per process so the upstream git remote cannot be DOS'd by a
  misbehaving SPA poll loop.

The 30-minute beat job `upgrade.check_latest` keeps the row warm in the
background.

## Backend changes

* `api/services/upgrade/__init__.py` — package marker; re-exports
  `remote_tags`, `state`.
* `api/services/upgrade/remote_tags.py` — anonymous git smart-HTTP
  discovery (`GET <url>/info/refs?service=git-upload-pack`) with pkt-line
  parser. Hardening:
  * URL must match the regex guard.
  * `localhost`, IMDS hostnames, and the IMDS IPv4/IPv6 are refused.
  * Response body capped at 4 MiB.
  * `mask_remote_url()` strips embedded `user:password@` from any URL
    before logging / SPA serialisation (forward-compat for the PR2 PAT
    flow).
  * Source of the URL is the `UPGRADE_GIT_REMOTE` env only — the
    docstring explicitly forbids accepting it from a request body to
    block SSRF if the surface ever expands.
* `api/services/upgrade/state.py` — Storage Table-backed `upgradestate`
  row with ETag CAS. Swappable backend so tests run without an Azure
  endpoint; `InMemoryBackend` refuses to construct unless
  `PYTEST_CURRENT_TEST` is set (or the explicit
  `ELB_ALLOW_INMEMORY_UPGRADE_STATE=true` opt-in). Lazy table-ensure so
  a transient Tables error doesn't block api sidecar startup. The
  schema intentionally omits an `error` field; it will land in PR3
  alongside the execution flow.
* `api/tasks/upgrade.py` — `check_latest_inline()` runs one discovery
  round (called both from the route and the beat); the
  `api.tasks.upgrade.check_latest` `@shared_task` wraps it for Celery.
  Logs (never the row) carry transient remote-fetch errors.
* `api/routes/upgrade.py` — three read-only endpoints behind
  `require_caller`. The `/check` endpoint enforces a process-wide
  15-second cooldown and returns `429 Too Many Requests` with
  `Retry-After` when violated. All responses mask the git remote URL.
* `api/main.py` — registers `upgrade.router` above the `frontend_proxy`
  catch-all. Also preserves route-supplied headers (e.g. `Retry-After`)
  in the `StarletteHTTPException` handler so the throttle response
  surfaces them — a small upstream fix that benefits any other route
  that needs a 429.
* `api/celery_app.py` — adds the `upgrade.check_latest` beat entry
  (30 min) and includes `api.tasks.upgrade` in the worker imports.

## Test changes

* `api/tests/test_upgrade_remote_tags.py` — 11 tests covering the
  pkt-line parser, capability stripping, peeled-tag suppression,
  semver sorting/filtering, response cap, URL guard, mask helper,
  and `UPGRADE_GIT_REMOTE` env handling.
* `api/tests/test_upgrade_state.py` — 6 tests exercising defaults,
  round-trip, mutate, public-dict serialisation, ETag CAS, and the
  JSON tolerator.
* `api/tests/test_upgrade_routes.py` — 11 tests covering all three
  endpoints: defaults, auth gate, configured/unconfigured candidates,
  remote failure, check mutation, throttle (429 + Retry-After), and
  credential masking on the candidates response.
* `api/tests/test_tasks_facade_contract.py` — no new contract entries;
  monkeypatches use the services-layer path
  (`api.services.upgrade.remote_tags.fetch_release_tags`) which is
  module-resolved at call time and survives the route layer's
  `from … import remote_tags`.

## Validation

* `uv run ruff check api/services/upgrade api/routes/upgrade.py api/tasks/upgrade.py api/tests/test_upgrade_*.py` — clean.
* `uv run pytest -q api/tests/test_upgrade_remote_tags.py api/tests/test_upgrade_state.py api/tests/test_upgrade_routes.py` — 28 passed.
* `uv run pytest -q api/tests` — 1114 passed (no regression vs prior 1109).
* No SPA changes in this PR; smoke-curl postponed to PR2 when the build
  surface lands.

## IaC / infra

No Bicep changes. No new RBAC. Existing user-assigned MI scopes
(`Contributor` on the workspace RG, `acrPull`/`acrPush`/`acrContributor`
on the platform ACR) already cover everything PR1 needs (Storage Tables
data plane + the future PR2 `az acr build`).

## Out of scope (deferred to later PRs)

* PR2 — terminal-sidecar `git clone` + `az acr build` pipeline that
  produces the new sidecar images. Includes the `git` allowlist
  extension in `terminal/exec_server.py`.
* PR3 — ARM PATCH of the Container App template (`apply`), rollout
  watcher, rollback, and escape-hatch command generator. Adds the
  `UpgradeAdmin` role guard and `error` field on the state row.
* PR4 — SPA UX (badge, modal, progress, rollback diff, retention
  countdown) and ACR-retention documentation guidance.
