# PR2 — self-upgrade build pipeline (2026-05-22)

## Motivation

PR1 added the read-only surface. PR2 lands the **build half** of the
self-upgrade flow: an operator clicks "Upgrade", and the deployed app
clones the requested git tag into the terminal sidecar and runs
`az acr build` for each of `elb-api`, `elb-frontend`, `elb-terminal`.
The ARM PATCH that swaps the Container App template to the new images
remains deferred to PR3 — PR2 stops in `state=succeeded` after the last
image is pushed, with no traffic impact. This means PR2 is **safe to
deploy** even though the visible UI (PR4) isn't there yet: the routes
just sit idle until something/someone POSTs `/api/upgrade/start`.

## User-facing change

Backend-only. Once the operator sets both `UPGRADE_GIT_REMOTE` (from
PR1) and `PLATFORM_ACR_NAME`, and lists their oid in
`UPGRADE_ADMIN_OIDS`, two new mutating endpoints become available:

* `POST /api/upgrade/start` — body `{target_version, target_sha?,
  confirm_downtime: true}`. Returns `202 Accepted` with the queued
  state row. Requires the `UpgradeAdmin` role gate. Refuses to start a
  second upgrade while one is in flight (`409 Conflict`). Refuses
  without `confirm_downtime=true` (`422`).
* `GET /api/upgrade/jobs/{job_id}/build-log/{component}` — streams the
  per-component Append Blob captured during the build. `component`
  must be `api`, `frontend`, or `terminal`. Admin-gated.

State row gains transitions: `idle → queued → fetching → building →
succeeded` for the happy path, and `→ failed_pre` for any pre-PATCH
failure (no customer impact, because no PATCH has been issued yet).

## Backend changes

* `terminal/exec_server.py` — `ALLOWED_BIN` gains `git`. The terminal
  sidecar now permits `git clone …` and `git -C … config …` invocations
  from the api/worker callers via the existing exec-token-gated
  loopback channel.
* `api/services/terminal_exec.py` — docstring updated to mirror the new
  allowlist.
* `api/services/upgrade/state.py` — adds `cas_state()` and
  `StateTransitionRefused` so transitions enforce a precondition. The
  `idle → queued` gate prevents two operators from racing into a
  parallel upgrade.
* `api/services/upgrade/auth.py` (new) — `require_upgrade_admin` FastAPI
  dependency. Admin signal is either an MSAL `roles` claim
  (`UpgradeAdmin`) or the caller oid appearing in `UPGRADE_ADMIN_OIDS`
  (comma-separated). The env path is the bootstrap so an operator with
  no App Registration changes can still use the feature.
* `api/services/upgrade/git_workspace.py` (new) — drives
  `git clone --depth 1 --single-branch --branch v<ver>` through the
  terminal sidecar, into the absolute path `/tmp/elb-upgrade/<job_id>`
  (outside the exec server's owned temp dir so the clone survives the
  request). After cloning it scrubs `remote.origin.url` via
  `git config` to strip any embedded credentials — forward-compat with
  the PR3 PAT flow.
* `api/services/upgrade/build_logs.py` (new) — Append Blob writer for
  per-component build logs (`upgrade-logs/<job_id>/build-<c>.log`).
  Swappable in-memory backend for tests; refuses to construct outside
  tests without the explicit opt-in env.
* `api/services/upgrade/image_builder.py` (new) — `build()` runs
  `az acr build --registry $PLATFORM_ACR_NAME --image elb-<c>:vA.B.0
  --file <dockerfile> <source_dir>` through `terminal_exec.stream`,
  forwarding every output line into the build log blob. Builds run
  sequentially by design; parallelisation lands in PR4.
* `api/tasks/upgrade.py` — adds `start_upgrade_inline` (CAS gate +
  enqueue) and `execute_upgrade` / `execute_upgrade_inline` (the worker
  pipeline). On any pre-PATCH failure the row is moved to
  `failed_pre` via CAS so a concurrent writer in a later state isn't
  overwritten.
* `api/routes/upgrade.py` — registers `POST /start` and
  `GET /jobs/{job_id}/build-log/{component}`. The start handler runs
  `start_upgrade_inline` which auto-rolls back to `idle` if the broker
  enqueue itself fails. Build-log responses pass blob bytes through as
  `text/plain` directly (no SAS).

## Test changes

* `api/tests/test_upgrade_git_workspace.py` — argv shape, exit-code
  handling, version/job_id validation, cleanup safety guard, and the
  credential-scrub round-trip (`x-access-token:supersecret@…` → masked
  back into the cloned repo's `remote.origin.url`).
* `api/tests/test_upgrade_build_logs.py` — name validation, append
  semantics, write_lines iterator, and the buffer-retention path on
  backend failure.
* `api/tests/test_upgrade_image_builder.py` — happy path argv + log
  capture, non-zero exit propagation, version/env guards, sequential
  iteration over the three components.
* `api/tests/test_upgrade_task.py` — full state-machine walk via the
  `enqueue` injection seam (no Celery worker required), double-start
  refusal (409 path), `failed_pre` on remote-unset, clone failure, and
  build failure.
* `api/tests/test_upgrade_routes.py` — `/start` admin gate,
  `confirm_downtime` enforcement, queued + enqueued result, second-call
  conflict, `/build-log` happy path, 404, 400 on invalid component,
  403 on missing admin.

## Validation

* `uv run ruff check api/services/upgrade api/routes/upgrade.py api/tasks/upgrade.py api/tests/test_upgrade_*.py terminal/exec_server.py` — clean.
* `uv run pytest -q api/tests/test_upgrade_*.py` — 57 passed.
* `uv run pytest -q api/tests` — 1143 passed (no regression vs prior 1114).
* End-to-end smoke deferred until PR3 wires the ARM PATCH; PR2's
  pipeline is exercised purely via unit tests because a real `az acr
  build` invocation takes minutes and depends on the platform ACR
  being reachable from the test environment.

## IaC / infra

No Bicep changes. RBAC already covers everything PR2 needs:

* `acrPush` (existing) — `az acr build` requires push on the registry.
* `Contributor` on the workspace RG (existing) — covers the Storage
  Blob append for build logs.

## Operator setup

To exercise the build pipeline once PR2 is deployed:

1. Set `UPGRADE_GIT_REMOTE` to the git remote that hosts the release
   tags (e.g. `https://github.com/<org>/elb-dashboard.git`).
2. Set `PLATFORM_ACR_NAME` to the platform ACR name (without the
   `.azurecr.io` suffix).
3. Set `UPGRADE_ADMIN_OIDS` to the comma-separated oid(s) permitted to
   start/rollback upgrades. A future PR replaces this with an App
   Registration role claim; the env stays as bootstrap.
4. From the SPA (or a curl), `POST /api/upgrade/start` with
   `{target_version, confirm_downtime: true}`. PR4 wires the modal.

## Out of scope (PR3 / PR4)

* PR3 — `aca_template` snapshot, `applier` (ARM PATCH), `rollout_watcher`,
  `rollback`, `escape_hatch`. Drives the actual revision swap and the
  `succeeded → rolling_out → succeeded | failed_rollout` transitions.
* PR4 — SPA UX (badge, modal, progress streaming, rollback diff,
  retention countdown). ACR retention guidance in docs/.
