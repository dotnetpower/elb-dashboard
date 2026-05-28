---
title: Settings — VNet peering "Apply NSG rule" hardening pass
description: 10-item self-review fixes for the Settings VNet-peering NSG action — preview/confirm UX, idempotency, audit, retries, per-NSG lock.
tags:
  - user-guide
  - operate
  - security
---

# Settings — VNet peering "Apply NSG rule" hardening pass

## Motivation

The first cut of [Settings → VNet peering Apply NSG rule](2026-05-28-settings-vnet-peering-apply-nsg-rule.md)
shipped the happy path. Self-review surfaced 10 gaps spanning
correctness, security, UX, robustness, and observability. This pass
addresses every one without changing the storage / data plane
contract: Storage stays `publicNetworkAccess: Disabled`, no new
identities, no new managed services.

## User-facing change

* **Preview before write.** The Settings panel now opens with a
  **"Preview NSG rule (80, 443)"** button. Clicking it runs a server-
  side dry-run that returns the *exact* rule the dashboard would
  write — name, priority, source CIDRs, destination `/32`, ports,
  protocol, NSG — rendered as a labelled grid. The operator then
  picks **Confirm & apply** (`btn-primary`) or **Cancel**
  (`btn-ghost`). The previous one-click write path is gone.
* **Deterministic CLI hint.** When the caller lacks permission, the
  copy-paste `az network nsg rule create` snippet now uses the same
  deterministic rule name the dashboard would have written (so two
  operators can no longer create duplicates with different names),
  with a single-line comment "`# Pick the first free priority in
  4000-4096; 4000 shown below.`".
* **Clipboard-blocked hint.** If `navigator.clipboard` is missing
  or rejects (sandboxed iframes, locked-down hosts), the panel now
  shows "Clipboard blocked — select the snippet and press Ctrl+C
  (or ⌘C) instead." instead of silently failing.
* **Audit trail.** Every Apply attempt (including dry-runs) is now
  recorded against the existing DB-ops audit log under
  `op = "nsg_apply"` (or `"nsg_apply_dry_run"`) with the target
  NSG name, destination IP, and a terminal event
  (`completed` / `skipped:<reason>` / `permission_denied` /
  `refused:<code>` / `failed:<class>`).
* **No more `500 internal-error`.** A previously-`assert`-guarded
  NSG-id parse-mismatch now returns a structured
  `500 nsg_id_parse_mismatch` and is logged, instead of crashing
  the worker.

## API diff

### Backend

* `api.tasks.azure.peering_nsg`:
  * `apply_inbound_allow_rule(..., dry_run: bool = False) -> ApplyResult` —
    new keyword argument. When `True`, every read still happens
    (so all idempotency / collision / no-free-priority branches
    return their normal verdict) but the terminal
    `begin_create_or_update` call is **not** issued and the result
    is returned with `applied=False, skipped_reason="dry_run",
    priority=<picked>` so the SPA can render the exact planned
    rule. Default is `False`, so every existing caller is
    unaffected.
  * `_existing_matches(...)` — now also rejects rules whose
    protocol is neither `Tcp`, `*`, nor `Asterisk`. Previously a
    same-scope UDP rule could mask a missing TCP allow.
  * `_summarise_rule(existing)` — returns the diagnostic shape
    used by `conflict_existing` (name, priority, protocol, access,
    direction, source/destination prefixes, destination ports).
    Replaces an ad-hoc dict in the `name_collision` branch.
  * `_retry_arm[T](fn, *, op_label, attempts=3, sleep=time.sleep)` —
    new in-module retry helper. Retries `ServiceRequestError` and
    `HttpResponseError` with status in `{408, 429, 500, 502, 503,
    504}`. Exponential backoff (1s/2s/4s, capped at 8s) +
    `random.uniform(0, 0.25)` jitter. Honours `Retry-After` /
    `retry-after` headers. All NSG list / get / write calls are
    now wrapped.
  * `deterministic_rule_name(aks_vnet_id, destination_ip) -> str` —
    public alias of `_deterministic_rule_name` so the route layer
    can build the CLI hint with the same name the helper would
    write.

* `api.routes.settings.vnet_peering`:
  * `POST /api/settings/vnet-peering/apply-nsg-rule` now accepts an
    optional `dry_run: bool` request field. The response carries
    two new optional fields: `planned_rule_name` (always) and
    `dry_run` (true when the request was a dry-run). The
    `skipped_reason` union gains `"dry_run"`.
  * The previously inline IPv4 / SSRF guard is now the shared
    `_validate_target_ip(target_ip, caller.object_id)` helper used
    by both `POST /peer` and `POST /apply-nsg-rule`. Behaviour
    matches the original guard line-for-line.
  * Concurrency: per-NSG `threading.Lock` (module-level dict
    keyed by full NSG resource id). Concurrent applies against
    the **same** NSG now serialise; applies against **different**
    NSGs still parallelise.
  * Audit: each apply records `record_db_op(op=…, caller=…,
    account_name="(peering-nsg)", db_name=<nsg-name>, extra=
    {"destination_ip":…})` at start and appends a terminal
    `record_db_op_event(…)` on every exit branch. Audit failures
    are caught and logged but never break the actual NSG
    operation.
  * `_nsg_cli_hint(..., aks_vnet_id: str)` — new required kwarg.
    The snippet now outputs `--name <deterministic_rule_name>`
    and prepends a one-line priority-range comment.

### Frontend

* `web/src/api/settings.ts`:
  * `VnetPeeringNsgRuleRequest.dry_run?: boolean`.
  * `VnetPeeringNsgSkipReason` union extended with `"dry_run"`.
  * `VnetPeeringNsgRuleResponse` adds optional
    `planned_rule_name?: string` and `dry_run?: boolean`.
* `web/src/components/SettingsPanel.tsx`:
  * `applyNsgRule(dryRun = true)` is the new entry point;
    `cancelNsgPreview()` clears the staged preview. Re-probe
    only fires on `!dryRun && response.applied`.
  * `NsgRuleAction` rewritten as a preview → confirm stepper.
    Initial button is "Preview NSG rule (80, 443)"; after a
    successful dry-run it renders the planned-rule grid plus
    **Confirm & apply** + **Cancel**.
  * `copied` state is now `"idle" | "ok" | "failed"`; the
    `"failed"` branch renders the clipboard-blocked hint.

## IaC diff

None. No Bicep, no infra, no identity changes.

## Storage / network posture

Unchanged. Storage stays `publicNetworkAccess: Disabled`; no SAS
tokens are issued; no public endpoint is opened; no production
code path flips Storage on. The NSG rule remains an *ingress*
rule on the **target workload subnet's** NSG, written from the
shared user-assigned MI when (and only when) the caller has
`networkSecurityGroups/securityRules/write`.

## Self-review checklist (10 items)

| # | Severity | Item | Resolution |
|---|----------|------|------------|
| 1 | High | `apply-nsg-rule` route bypassed the `peer` route's IPv4 / SSRF guard | Both routes now share `_validate_target_ip` |
| 2 | High | Bare `assert` for NSG-id parse mismatch crashed the worker | Returns `500 nsg_id_parse_mismatch` and logs |
| 3 | High | `_existing_matches` ignored protocol — UDP rule could mask missing TCP allow | Protocol gate: `{Tcp, *, Asterisk}` |
| 4 | High | No audit trail | Every attempt records `record_db_op` start + terminal `record_db_op_event` |
| 5 | Med | CLI hint used a placeholder name and no priority guidance | Deterministic name + "Pick the first free priority in 4000-4096" comment |
| 6 | Med | `conflict_existing` was an ad-hoc dict | `_summarise_rule` produces the same shape the dashboard would have written |
| 7 | Med | One-click apply with no preview | Server-side `dry_run` + SPA Preview → Confirm flow |
| 8 | Low | Two simultaneous applies could race the priority pick | Per-NSG `threading.Lock` |
| 9 | Low | `nsgResult` / `nsgError` could persist across reopens; clipboard failure was silent | `cancelNsgPreview` resets state; clipboard failure renders an explicit hint |
| 10 | Low | One transient ARM hiccup failed the whole apply | `_retry_arm` with backoff + `Retry-After` |

## Validation evidence

* Focused suites: `uv run pytest -q api/tests/test_peering_nsg.py
  api/tests/test_settings_vnet_peering.py` → **41 passed in 3.56s**
  (14 newly added: 6 in `test_peering_nsg.py`, 5 in
  `test_settings_vnet_peering.py`, plus 3 retry helper tests).
* Wide backend sweep: `uv run pytest -q api/tests` →
  **1744 passed, 3 skipped, 1 failed**. The single failure is
  `test_terminal_exec.py::test_run_truncates_stdout_above_cap`
  (parallel-worker `EXEC_RUN_MAX_OUTPUT_BYTES` timing on a slow
  host — passes in isolation in 11.4 s). Unrelated to this
  change; no `terminal_exec` code was touched.
* Lint: `uv run ruff check api` → **All checks passed!**
* Frontend: `cd web && npm run build` → ✓ built in 6.88 s, no
  TypeScript errors, no new bundle warnings.
* Docs guard: `uv run python scripts/docs/check_frontmatter.py` →
  **OK — frontmatter guard checked 48 navigated pages.**
* Consumer search (`apply_inbound_allow_rule`, `_nsg_cli_hint`,
  `deterministic_rule_name`, `_validate_target_ip`,
  `VnetPeeringNsgRuleResponse`, `VnetPeeringNsgSkipReason`,
  `VnetPeeringNsgRuleRequest`): every match is either inside
  this change's surface (route, helper, tests, SPA) or the
  facade-contract guard `api/tests/test_tasks_facade_contract.py`
  which only checks attribute resolvability — unaffected by the
  new optional `dry_run` kwarg.
