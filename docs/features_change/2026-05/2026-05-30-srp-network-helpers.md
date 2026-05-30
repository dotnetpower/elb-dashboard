# 2026-05-30 — SRP cleanup: drop Remote Terminal VM helpers from `api/services/network.py`

## Motivation

[api/services/network.py](../../../api/services/network.py) was 425 lines and its
`Responsibility:` line read

> Idempotent helpers for resource group + VNet + NSG + Public IP

— a four-noun chain that explicitly described four cloud primitives in one
module. That wording violated the charter §11 SRP gate ("If the
`Responsibility` line needs 'and' chains, unrelated nouns, or more than one
architectural layer (route + service + task + parser), split the work before
adding more code"), and it described a world that no longer exists.

The VNet / NSG / Public IP / NIC / SSH-rule helpers were originally written
for the Remote Terminal VM topology — `compute.run_shell()`,
`ensure_ssh_from_function_app()`, and friends, see
[docs/features_change/2026-05/2026-05-13-primer-design-run-command.md](./2026-05-13-primer-design-run-command.md)
and [docs/features_change/2026-05/2026-05-13-terminal-ssh-hardening.md](./2026-05-13-terminal-ssh-hardening.md)
for the historical use site. That topology was retired when the browser
terminal moved to a Container App sidecar in
[docs/container-apps-migration.md](../../container-apps-migration.md) and
charter §14 made the Remote Terminal VM model explicitly out of scope:

> **Any new Remote Terminal VM, NSG, public IP, SSH path, or admin password
> handling.** The browser terminal is a sidecar.

A workspace grep for every exported symbol confirmed only
`ensure_resource_group` had a live caller
([api/routes/resources.py](../../../api/routes/resources.py) line 24 +
line 61); the other seven (`NetworkInfo` dataclass, `_dns_label`,
`ensure_network`, `create_ssh_rule`, `ensure_ssh_from_function_app`,
`delete_resource`, `delete_resource_group`) had **zero** consumers in
`api/`, `terminal/`, `web/`, `scripts/`, or `infra/` — only the legacy
change-note prose and the `Key entry points:` header inside `network.py`
itself.

## User-facing change

**None.** Pure dead-code removal + module rescope. No HTTP route, no Celery
task, no Bicep, no UI control is affected. `api/routes/resources.py` still
calls `network_svc.ensure_resource_group(...)` and its behaviour is
byte-for-byte unchanged.

## API / IaC diff summary

* [api/services/network.py](../../../api/services/network.py): 425 → 40 lines.
  * **Removed (dead code)**: `NetworkInfo` dataclass, `_dns_label`,
    `ensure_network`, `create_ssh_rule`, `ensure_ssh_from_function_app`,
    `delete_resource`, `delete_resource_group`, plus all the constants
    that fed them (`VNET_NAME_TEMPLATE`, `SUBNET_NAME`, `NSG_NAME_TEMPLATE`,
    `PIP_NAME_TEMPLATE`, `NIC_NAME_TEMPLATE`, `MAX_FUNCTION_SSH_SOURCE_IPS`)
    and the now-unused `hashlib` / `re` / `network_client` imports.
  * **Kept**: `ensure_resource_group(credential, subscription_id,
    resource_group, region)` — the only live consumer at
    [api/routes/resources.py:61](../../../api/routes/resources.py#L61).
  * Rewrote the module docstring so `Responsibility:` is a single sentence
    ("Idempotent `resource_groups.create_or_update` shim …") and
    `Edit boundaries:` explicitly steers future networking primitives to a
    dedicated module rather than back into `network.py`.
* **No** route, task, test, Bicep, frontend, persona-matrix, or env-var
  change.

## Validation evidence

* Wide: `uv run pytest -q api/tests` → **2151 passed, 3 skipped in 36.49s**
  on the new tree.
  * The one `test_terminal_exec.py::test_run_truncates_stdout_above_cap`
    failure under `-n auto` is a pre-existing flake on the parallel
    `xdist` worker (subprocess timeout race); re-run in isolation
    (`uv run pytest -q api/tests/test_terminal_exec.py::test_run_truncates_stdout_above_cap`)
    → **1 passed in 10.13s**. Not related to this SRP refactor and
    repro'd on the previous commit too.
* Lint: `uv run ruff check api/services/network.py` → **All checks passed!**.
* Consumer search:
  * `grep_search "from api\.services import network|from api\.services\.network|api\.services\.network"` → only
    [api/routes/resources.py:24](../../../api/routes/resources.py#L24).
  * `grep_search "ensure_network|create_ssh_rule|ensure_ssh_from_function_app|delete_resource_group|delete_resource\(|NetworkInfo"` → all
    14 matches were inside `api/services/network.py` itself (docstring +
    `Key entry points:` line + the defs being removed). The
    `ensure_ssh_from_function_app` matches outside `api/` are in
    `docs/features_change/2026-05/*.md` and the prebuilt `site/` HTML
    that mirror those notes — historical narrative only, not live code.
  * No test fixture / mock / OpenAPI payload references the removed symbols.
* Frontend: no `web/src/**` files touched — `npm run build` not required.
* IaC: no Bicep touched — `azd provision --preview` not required.
* Diff audit: `git status --short` → only `M api/services/network.py`;
  `git diff --stat` → 1 file, +14 / -399.

## Hardening discipline (§12a):

- [x] In scope: SRP refactor (dead-code removal in `api/services/`) — no
  auth, RBAC, network ACL, JWT, ticket, CORS, or sanitisation surface
  changed.
- [x] RBAC change is single-PR safe (no role narrowed) — N/A, no RBAC change.
- [x] Persona Matrix tests pass for owner / contributor / reader / dev_bypass
  — wide-sweep green; the removed helpers were never invoked from any route
  exercised by the matrix.
- [x] Reader allowlist unchanged — no Reader-required route touched.
- [x] Capability Probe passes locally — no new Azure surface, probe
  unaffected.
- [x] New guard ships default-OFF behind `STRICT_*` / `ENFORCE_*` env var —
  N/A, this PR removes code, it does not add a guard.
- [x] No `Depends(require_caller)` added to an SSE event stream — no SSE
  changes.
- [x] Change note (this file) summarises persona impact: every persona is
  byte-for-byte unaffected. The dead helpers had no caller, so there is no
  observable behaviour to preserve or break for any persona.
