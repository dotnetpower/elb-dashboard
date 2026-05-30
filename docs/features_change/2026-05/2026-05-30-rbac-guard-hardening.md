# RBAC removal preflight — 30-point hardening wave

**Date**: 2026-05-30
**Type**: refactor(security) — improvements to the existing PR-SD-1 guard, no
new feature surface, no charter rule additions
**Scope**: `scripts/dev/check_rbac_removal.py`,
`scripts/dev/preflight_rbac_removal.sh`,
`api/tests/test_check_rbac_removal.py`, `.github/copilot-instructions.md` §12a
Rule 7

## Motivation
PR-SD-1 shipped the baseline RBAC removal halt (charter §12a Rule 7) on
2026-05-30 in warn-only mode. Before the planned dogfood / strict-flip we
audited the guard for edge cases that could either (a) silently bypass the
halt or (b) make the halt output unactionable for the on-call operator. This
wave addresses 30 such items grouped into four phases — parser robustness,
parser UX, wrapper safety/visibility, and test coverage — plus a charter
scope-note refresh.

## User-facing change
Operators running `bash scripts/dev/preflight_rbac_removal.sh` (or
`azd provision` with the preprovision hook) now see:

* a **mode banner** at the start (`mode=STRICT` vs `mode=WARN-ONLY`)
* **indexed finding lines** (`[1/3]`, `[2/3]`, …)
* **built-in role names** alongside the GUID
  (`role=Owner (8e3af657-…)`, `role=Storage Blob Data Contributor (ba92f5b4-…)`)
* an **audit-grep echo** of an accepted `ACCEPT_RBAC_REMOVAL` token
* a **`SUMMARY:` sentinel line** at the bottom of the output
* a **final 1-line wrapper summary** with the exit code, status keyword, and
  wall-clock elapsed time

Operators running with `--mask-principals` (e.g. when uploading the preflight
output to a CI artifact store) now see `***-7777`-style masked principal ids
instead of full GUIDs.

## API/IaC diff summary

### Parser (`scripts/dev/check_rbac_removal.py`)
1. `load_whatif` wraps file I/O + JSON parse in `try/except` and raises
   `SystemExit(EXIT_AZ_FAILED)` (or `EXIT_BAD_ENV` for `FileNotFoundError`)
   instead of letting the raw exception bubble up.
2. `_unwrap_changes` walks up to two `properties` levels — the previous
   implementation handled `{"changes": …}` and `{"properties": {"changes": …}}`
   but not the doubly-wrapped SDK shape.
3. New `_BUILTIN_ROLE_GUIDS` dict (19 entries — Owner, Contributor, Reader,
   User Access Administrator, Storage Blob Data Contributor/Reader, Storage
   Queue/Table Data Contributor, AcrPull/AcrPush, all Key Vault built-ins, the
   two Log Analytics roles, and Azure Container Apps Operator). The list is
   intentionally focused on roles `infra/modules/*.bicep` actually grants today.
4. `summarise_change` gained `index=` / `total=` kwargs so the main loop can
   prefix each finding with `[i/N]`.
5. `summarise_change` gained `mask_principals=True` so CI artifacts can ship
   without full principal id exposure.
6. `summarise_change` handles `principalType=None` explicitly via
   `"<unknown-type>"` (the previous code would emit `None`).
7. `summarise_change` handles missing `roleDefinitionId` via `<unknown-role>`.
8. `role_name_for_guid` public helper added (case-insensitive GUID lookup).
9. `_TRUTHY_VALUES` extended to include `"enabled"` (Azure-style truthy).
10. `compute_whatif`: new `--template-file` existence check; raises
    `SystemExit(EXIT_BAD_ENV)` when the path is missing.
11. `compute_whatif`: new `runner=` kwarg accepting a `subprocess.run`-shaped
    callable so unit tests can mock the az invocation without
    `monkeypatch.setattr(subprocess, ...)`.
12. `compute_whatif`: JSON parse failure on az stdout maps to
    `SystemExit(EXIT_AZ_FAILED)` (was previously letting `JSONDecodeError`
    propagate).
13. `main`: emits a `SUMMARY:` line on every code path (OK, WARN-ONLY,
    ACCEPTED, HALT) so the outcome is visible at the bottom of long logs.
14. `main`: echoes the accepted `ACCEPT_RBAC_REMOVAL` token verbatim so
    `git log + grep` can later answer "who acknowledged removal X, when?".
15. Argparse description + epilog with a **3-example usage block** and an
    **exit-code table** so `--help` is operator-actionable.
16. Module docstring extended with the explicit exit-code table that was
    previously buried in code comments.

### Wrapper (`scripts/dev/preflight_rbac_removal.sh`)
17. `umask 077` early — the resolved parameters file may contain principal
    ids and should not be world-readable.
18. `trap` extended from `EXIT` to `EXIT INT TERM ERR`.
19. New `cleanup_and_summarise` trap that prints a 1-line final summary with
    `exit=`, `status=`, and `elapsed=Ns` regardless of code path.
20. New `strict_or_skip` helper: in `STRICT_RBAC_REMOVAL_HALT=true` mode
    every internal failure (missing env, missing `az`, missing template,
    what-if call failed) now exits 3 with `status=halt-internal-failure`
    instead of silently skipping. In WARN-ONLY mode the legacy silent-skip
    behaviour is preserved so day-to-day development is never blocked.
21. PY-interpreter detection: `-e` + a `python -c ''` probe replaces the
    bare `-x` check, so a broken `.venv/bin/python` symlink no longer
    blocks the script.
22. Mode banner printed up front (`mode=STRICT (...)` vs `mode=WARN-ONLY
    (... default-OFF)`).
23. ACCEPT-token presence noted at startup (`ACCEPT_RBAC_REMOVAL is set ...`).
24. envsubst-absent warning rewritten to point at the install command on
    Debian/macOS and to call out that placeholders will be sent to az
    unresolved.
25. New "unresolved placeholders" detector: if `envsubst` leaves any
    `${VAR}`-style tokens in the resolved parameters file, the wrapper
    warns with the first 5 unresolved variable names so the operator can
    `azd env set …` them before re-running.
26. Parser exit code 4 (az/JSON failure) now mapped to HALT in strict mode
    instead of soft skip, matching item #20.
27. `FINAL_STATUS` enum (`ok`, `halt`, `halt-internal-failure`,
    `parser-failure-skipped`, `unexpected-rc-N`, `skipped`) so the final
    summary line is greppable for distinct outcomes.

### Tests (`api/tests/test_check_rbac_removal.py`)
28. 22 new test cases covering: doubly-wrapped envelope, built-in role
    name lookup (Owner / Contributor / Storage Blob Data Contributor +
    case-insensitivity + unknown), principal masking, indexed line prefix,
    SUMMARY sentinel on all four code paths, missing `--from-json` file
    → exit 2, malformed JSON → exit 4, argparse unknown flag → exit 2,
    argparse missing required source → exit 2, `compute_whatif` happy
    path with `runner=` mock asserting argv shape, `compute_whatif` az
    failure → exit 4, `compute_whatif` missing template → exit 2,
    `compute_whatif` malformed stdout → exit 4, `--help` epilog contains
    the exit-code table, accepted ACCEPT token echoes verbatim into stdout.
29. `test_is_strict_enabled` parametrize extended with `"enabled"` /
    `"ENABLED"` / `"disabled"` cases.

### Charter (`.github/copilot-instructions.md` §12a Rule 7)
30. Added "Scope notes" sub-section: (a) the guard scans `main.bicep` only
    because `az` flattens nested modules; future second subscription-scope
    entry points must extend the wrapper, (b) STRICT mode now halts on
    internal failures, (c) local validation hint with the
    `STRICT_RBAC_REMOVAL_HALT=true` rehearsal command.

## Validation evidence

### Focused tests
```
$ uv run pytest -q api/tests/test_check_rbac_removal.py
66 passed in 3.72s
```
(44 from PR-SD-1 + 22 new, all green.)

### Lint
```
$ uv run ruff check scripts/dev/check_rbac_removal.py api/tests/test_check_rbac_removal.py
All checks passed!
```

### End-to-end smoke (parser, three code paths against a 2-removal fixture)
```
--- warn-only mode ---
[rbac-guard] detected 2 roleAssignment deletion(s) in what-if:
  [1/2] principal=11111111-…-7777 (ServicePrincipal) role=Storage Blob Data Contributor (ba92f5b4-…) scope=/subscriptions/… resourceId=…
  [2/2] principal=22222222-…-8888 (ServicePrincipal) role=Owner (8e3af657-…) scope=/subscriptions/… resourceId=…
[rbac-guard] WARN: STRICT_RBAC_REMOVAL_HALT is OFF — proceeding without halt.
[rbac-guard] SUMMARY: 2 removal(s) detected (WARN-ONLY, allowed)
exit=0

--- strict + accept + --mask-principals ---
  [1/2] principal=***-7777 (ServicePrincipal) role=Storage Blob Data Contributor (ba92f5b4-…) …
  [2/2] principal=***-8888 (ServicePrincipal) role=Owner (8e3af657-…) …
[rbac-guard] ACCEPT_RBAC_REMOVAL satisfied (token='phase-2-of-pr-99'); proceeding with deployment.
[rbac-guard] SUMMARY: 2 removal(s) detected (ACCEPTED, allowed)
exit=0

--- strict, no accept ---
[rbac-guard] ERROR: STRICT_RBAC_REMOVAL_HALT is ON and ACCEPT_RBAC_REMOVAL is not set. Refusing to deploy.
[rbac-guard] SUMMARY: 2 removal(s) detected (HALT)
exit=3
```

### Wrapper smoke (mode banner + strict_or_skip safety)
```
$ unset AZURE_SUBSCRIPTION_ID AZURE_LOCATION; bash scripts/dev/preflight_rbac_removal.sh
[20:56:15] rbac-guard: mode=WARN-ONLY (STRICT_RBAC_REMOVAL_HALT unset; charter §12a Rule 4 default-OFF)
[20:56:15] rbac-guard: AZURE_SUBSCRIPTION_ID or AZURE_LOCATION unset — skipping preflight (warn-only mode).
[20:56:15] rbac-guard: preflight complete (exit=0, status=skipped, elapsed=0s)
exit=0

$ STRICT_RBAC_REMOVAL_HALT=true bash scripts/dev/preflight_rbac_removal.sh
[20:56:15] rbac-guard: mode=STRICT (STRICT_RBAC_REMOVAL_HALT=true)
[20:56:15] rbac-guard: STRICT mode and AZURE_SUBSCRIPTION_ID or AZURE_LOCATION unset — refusing to skip preflight.
[20:56:15] rbac-guard: preflight complete (exit=3, status=halt-internal-failure, elapsed=0s)
exit=3
```

## Hardening discipline (§12a)
- [x] In scope: rbac
- [x] RBAC change is single-PR safe — no role narrowed; this PR only
  polishes the guard that detects narrowings.
- [x] Persona Matrix tests untouched (no auth surface modified).
- [x] Reader allowlist unchanged.
- [x] Capability Probe untouched (no role assignment changed).
- [x] RBAC removal preflight green locally — wrapper smoke above shows
  warn-only `exit=0, status=skipped` and strict `exit=3,
  status=halt-internal-failure`.
- [x] New guard ships default-OFF — N/A, this PR only polishes the
  existing PR-SD-1 guard which is already default-OFF
  (`STRICT_RBAC_REMOVAL_HALT` unset = warn-only).
- [x] No `Depends(require_caller)` added to an SSE event stream.
- [x] Change note (this file) summarises persona impact: zero — all
  changes are additive to the same warn-only output that already
  shipped, plus a strict-mode safety tightening that only triggers
  when an operator opts in via `STRICT_RBAC_REMOVAL_HALT=true`.

## Out of scope (deferred)
- Wiring a `--from-deployment <existing-name>` mode that reuses a previous
  what-if document. The az CLI does not expose this directly today; the
  workaround (`az deployment sub show --name <n> --query …`) does not
  surface the `changes[]` array. Skip until a clear need arises.
- Flipping `STRICT_RBAC_REMOVAL_HALT` default to ON. Per Rule 4 this needs
  one full release cycle of warn-only dogfood plus a green
  `pytest -q api/tests/test_check_rbac_removal.py` with the gate forced
  ON before the flip-PR can land.
