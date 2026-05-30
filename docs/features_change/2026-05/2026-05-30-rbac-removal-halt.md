# 2026-05-30 — RBAC removal halt at `azd provision` (charter §12a Rule 7)

## Motivation

User reported a recurring pain: when re-running `azd provision` (or
`azd up`) on an existing deployment after a security/hardening PR
landed, role assignments would silently disappear and an Owner /
Contributor / Reader operator would start seeing
`403 AuthorizationFailed` in production. The discipline already lived
in the charter (§12a Rule 1 — 2-phase ADD-then-REMOVE with 7-day soak)
but it was human-only — a one-character Bicep diff that removed a
`roleAssignments` resource (or removed the module that owns it from
`infra/main.bicep`) was indistinguishable from any other refactor
in PR review.

Bicep is declarative, so any such removal causes the next
`az deployment sub create` (which is what `azd provision` runs) to
**DELETE** the live assignment. The first symptom is `403` on the
runtime data plane — by then the deploy already shipped.

Rule 1 (process) plus the existing Rules 2-6 (Persona Matrix, Capability
Probe, default-OFF guards, SSE ticket exception, PR template) are not
enough on their own. The missing piece was a machine-checked preflight
that runs **before** `azd provision` applies the template, and refuses
to proceed when a roleAssignment Delete is queued unless the operator
explicitly acknowledges it with a phase-2 PR reference.

## User-facing change

* **`azd provision` / `azd up`** now runs an RBAC removal preflight in
  the existing `preprovision` hook, before the Bicep template is
  applied. The preflight:
  * Calls `az deployment sub what-if --no-pretty-print --output json`
    against `infra/main.bicep` with the resolved
    `infra/main.parameters.json`.
  * Reports every `Microsoft.Authorization/roleAssignments` entry
    whose `changeType` is `Delete` or `DeploymentMode` (complete-mode
    removal), printing scope / role definition guid / principal id /
    resource id.
* **Default behaviour is warn-only** (per charter §12a Rule 4 — every
  new guard ships default-OFF). Findings show in the preprovision log
  but `azd provision` proceeds.
* **Opt-in halt**: set `STRICT_RBAC_REMOVAL_HALT=true` for the
  `azd provision` invocation and the preflight exits non-zero on any
  finding, which aborts the deployment before ARM is touched.
* **Phase-2 acknowledgement**: when `STRICT_RBAC_REMOVAL_HALT=true`
  and the removal is intentional (a Rule 1 phase-2 PR that drops the
  broader role after the soak window), set
  `ACCEPT_RBAC_REMOVAL='phase-2-of-pr-<N>'` (regex tolerates
  `phase-2 of 2 (see PR-<N>)`, `phase-2 of 2 (see #<N>)`, etc.). The
  preflight logs the override and proceeds.

No production runtime behaviour changes. The Container App template,
runtime RBAC, and dashboard surfaces are untouched. The guard only
fires during `azd provision` preprovision.

## API / IaC diff summary

* **New file** [scripts/dev/check_rbac_removal.py](../../../scripts/dev/check_rbac_removal.py)
  (≈260 lines): standalone Python script with no Azure SDK dependency
  (stdlib JSON only). Public surface:
  * `find_rbac_removals(whatif: dict) -> list[dict]` — parses both the
    bare CLI shape `{"changes": [...]}` and the wrapped
    `{"properties": {"changes": [...]}}` envelope.
  * `summarise_change(change: dict) -> str` — extracts principal id,
    principal type, role definition guid, and scope for a per-finding
    log line.
  * `is_strict_enabled(env: dict) -> bool` — Rule 4 gate
    (`STRICT_RBAC_REMOVAL_HALT` truthy values).
  * `is_acceptance_valid(token: str) -> bool` — regex matcher for the
    documented acknowledgement patterns.
  * `main(argv, env) -> int` — CLI with `--from-json FILE` (or `-` for
    stdin) and `--compute --subscription SID --location LOC` modes.
    Exit codes: `0` OK / warn / accepted, `2` bad CLI usage, `3` HALT,
    `4` az / JSON parse failure.
* **New file** [scripts/dev/preflight_rbac_removal.sh](../../../scripts/dev/preflight_rbac_removal.sh)
  (≈100 lines): the azure.yaml-friendly wrapper. Skips silently when
  `AZURE_SUBSCRIPTION_ID` / `AZURE_LOCATION` are unset (so unit-test
  contexts do not break), substitutes `${AZURE_*}` placeholders in
  `infra/main.parameters.json` via `envsubst`, invokes
  `az deployment sub what-if`, and delegates parsing to the python
  script. Treats `az` failures as non-fatal (skip) so a what-if outage
  cannot block deploys.
* [azure.yaml](../../../azure.yaml): one new line in the `preprovision`
  hook calls `bash ./scripts/dev/preflight_rbac_removal.sh` just before
  the "Bicep provision" progress note. The hook continues to honour
  `continueOnError: false`, so a HALT exit aborts `azd provision`.
* [.github/copilot-instructions.md](../../../.github/copilot-instructions.md):
  added §12a Rule 7 ("RBAC removal halt at `azd provision`")
  documenting the gate semantics, the env-var names, the default-OFF
  transition plan, and the cross-reference to the Rule 6 PR template.
  Rule 6 checklist now includes a new item: "RBAC removal preflight
  green locally (`scripts/dev/preflight_rbac_removal.sh`) OR
  `ACCEPT_RBAC_REMOVAL=phase-2-of-pr-<N>` recorded in the PR
  description".
* **New file** [api/tests/test_check_rbac_removal.py](../../../api/tests/test_check_rbac_removal.py)
  (44 tests): covers parser shapes (empty changes, wrapped envelope,
  non-roleAssignment filter, Create/Modify/NoChange/Ignore/Deploy
  filter, Delete/DeploymentMode/case insensitivity, garbage input),
  `summarise_change` extraction, `is_strict_enabled` truthy matrix,
  `is_acceptance_valid` regex matrix (documented patterns + rejects),
  and `main()` exit-code matrix (OK / warn-only / halt without accept /
  halt with garbage accept / accept satisfied / stdin / `--compute`
  validation).

## Validation evidence

```
$ uv run pytest -q api/tests/test_check_rbac_removal.py 2>&1 | tail -3
............................................                             [100%]
44 passed in 2.37s
```

Self-review checklist (per charter §13 "Post-implementation self-review"):

* Consumer search: `check_rbac_removal.py` is a new standalone tool with
  no callers in `api/` or `web/`. The `preflight_rbac_removal.sh`
  wrapper has exactly one caller (`azure.yaml` preprovision); grep for
  `preflight_rbac_removal` confirms no other references.
* Backward compatibility: default behaviour unchanged
  (`STRICT_RBAC_REMOVAL_HALT` unset = warn-only). Existing `azd up`
  flows on subscriptions that do not have role assignments to remove
  see exactly one extra log line per provision.
* Wide test sweep: `uv run pytest -q api/tests` (run separately, see
  commit log) — only the new 44 tests are affected.
* Lint: `uv run ruff check scripts/dev/check_rbac_removal.py` clean
  (script follows the project header convention).
* Diff audit: 5 files touched
  (`scripts/dev/check_rbac_removal.py` new, `scripts/dev/preflight_rbac_removal.sh`
  new, `azure.yaml` 1-line insertion, `.github/copilot-instructions.md`
  Rule 7 addition + Rule 6 checklist item, `api/tests/test_check_rbac_removal.py`
  new). No unrelated dirty files.
* Fixture / mock parity: no existing fixtures reference role-assignment
  what-if shapes; new fixtures live inline in the test file.

## Hardening discipline (§12a) checklist

- [x] In scope: `rbac`
- [x] RBAC change is single-PR safe (this PR only adds a guard — does
      not narrow any role)
- [x] Persona Matrix tests pass for owner / contributor / reader /
      dev_bypass (no auth surface touched)
- [x] Reader allowlist unchanged
- [x] Capability Probe passes locally
      (`scripts/dev/probe_capabilities.py`)
- [x] RBAC removal preflight green locally
      (`scripts/dev/preflight_rbac_removal.sh` — N/A for this PR
      because the PR itself does not change Bicep, but the script
      exists and has 44 passing tests)
- [x] New guard ships default-OFF behind `STRICT_RBAC_REMOVAL_HALT`
      env var
- [x] No `Depends(require_caller)` added to an SSE event stream
- [x] This change note documents the user-visible impact

## Follow-ups (deferred)

* **Rule 8 (Persona smoke against live tenant)** — proposed but
  deferred. Cost (2 dedicated UAMIs in a sandbox sub) is non-trivial
  and the value depends on Rule 7 catching the bulk of regressions
  first. Re-evaluate after one week of Rule 7 in dogfood.
* **Rule 9 (snapshot/rollback for role assignments)** — proposed but
  deferred. Strong overlap with Rule 7 detection; only useful if a
  removal slips past Rule 7 in the warn-only window. Re-evaluate at
  the same checkpoint as Rule 8.
* **Default flip of `STRICT_RBAC_REMOVAL_HALT=true`** — its own PR
  after one full release cycle of dogfood with the warning visible in
  preprovision logs and a green Persona Matrix run with the gate
  forced ON.
