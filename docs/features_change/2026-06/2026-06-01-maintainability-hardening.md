# Maintainability hardening: shared env-loader, azure.mgmt factory consolidation, feature-gate registry

## Motivation

A project-structure review surfaced five maintainability items. This change
delivers the safe subset and documents the deferral rationale for the two large
refactors that are unsuitable for an autonomous pass.

1. **Bash env-loader regression** — `cli-upgrade.sh`, `quick-deploy.sh`'s
   `load_azd_env`, `setup-gha-oidc.sh`, and `local-run.sh` carried the
   `${!key:-}` guard, which treats an explicit empty-string export the same as
   unset. This is the exact incident class that twice leaked dev values
   (`VITE_API_BASE_URL=""`, `VITE_AUTH_DEV_BYPASS=true`) into cloud frontend
   builds.
2. **`azure.mgmt.*` imported directly in routes and tasks** — charter §11 says
   routes and Celery tasks must call Azure SDK through `api/services/` wrappers.
   `SubscriptionClient`, `AuthorizationManagementClient`, and
   `ManagedServiceIdentityClient` were constructed inline in several route and
   task modules.
3. **No single registry of the env-var feature gates** — operators had to grep
   the codebase to learn what `STRICT_*` / `ENFORCE_*` / `ELB_ALLOW_*` do and
   what their defaults are.

## User-facing change

No runtime behaviour change. This is internal maintainability work plus one new
documentation page.

- New operator doc: **Feature Gate Registry**
  ([docs/operate/feature-gates.md](../../operate/feature-gates.md)) listing every
  hardening gate, escape hatch, and local-debug switch with its default and
  effect. Wired into the Operate section of the docs nav.

## API / IaC diff summary

- **New** `scripts/dev/lib-env.sh` — the single correct implementation of
  `.env` / `azd env get-values` import using the `${!key+x}` (set-vs-unset)
  guard, with a re-source guard. `quick-deploy.sh`, `cli-upgrade.sh`, and
  `setup-gha-oidc.sh` now source it instead of carrying their own copies;
  `local-run.sh`'s bespoke allowlist parser was fixed in place
  (`${!key:-}` → `${!key+x}`).
- **New** `scripts/dev/tests/test_lib_env.sh` — regression test asserting that
  an explicit empty-string export survives import, an unset var is imported, a
  pre-set value is not overwritten, and the skip-list is honoured.
- **New** factories in
  [api/services/azure_clients.py](../../../api/services/azure_clients.py):
  `subscription_client(cred)`, `authorization_client(cred, sub)`,
  `msi_client(cred, sub)`. They use lazy (call-time) imports on purpose so the
  existing tests that monkeypatch the class on its `azure.mgmt.*` module keep
  working.
- **Updated call sites** to use the factories: `routes/arm.py` (×2),
  `routes/health.py`, `routes/me.py`, `tasks/azure/rbac.py` (×4),
  `tasks/azure/peering_nsg.py`, `tasks/openapi/rbac.py`. The
  `RoleAssignmentCreateParameters` model import (a data class, not an SDK call)
  is left at the call site. `api/services/**` modules already are the wrapper
  layer and keep their direct imports.

## Deferred (recommended as separately-reviewed PRs)

- **Backend module-split Phase C is already 4/5 complete** — `state_repo.py`,
  `taxonomy/`, `monitoring/`, and `storage/data.py` are all already facade +
  subpackage splits. The earlier review report was stale.
- **`api/services/k8s/monitoring.py` (~1161 LOC) split — deferred.** The Phase C
  design doc itself flags this module as the most complex and
  autonomous-unsuitable: it has a wide monkeypatch-by-name surface
  (`_get_k8s_session`, `k8s_get_pods`, `k8s_get_service_ip`,
  `k8s_get_deployment_env_value`, `k8s_release_warmup_cache`,
  `k8s_ready_warmup_node_names`) that requires call-site facade resolution. It is
  already well-organised (delegates to sibling helpers). Risk/reward is poor for
  an autonomous pass.
- **`web/src/components/SettingsPanel.tsx` (~3723 LOC) split — deferred.** The
  frontend has a weak visual-regression safety net, so a large component split
  carries real risk without manual review.

## Validation evidence

- `uv run ruff check api` → **All checks passed!**
- `uv run pytest -q api/tests` → **2378 passed, 3 skipped** (the 116 tests that
  monkeypatch the consolidated `azure.mgmt` clients —
  `test_azure_tasks.py`, `test_openapi_deploy_contract.py`, `test_me_route.py`,
  `test_smoke.py` — all green).
- `bash scripts/dev/tests/test_lib_env.sh` → all assertions pass; `bash -n` on
  all six modified/sourced shell scripts is clean.
- `uv run python scripts/docs/check_frontmatter.py` → OK, 52 navigated pages.
- `DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict` → built
  successfully (new page wired into nav; no orphan-page failure).
