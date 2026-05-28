# 2026-05-29 — PermissionGate wave 2, BLAST submit sanitisation, quick-deploy preflight (#6 / #7 / #8)

## Motivation

Third commit on the critique-fix arc. Three independent improvements
bundled because they are small, all backed by tests, and naturally
land together (RBAC affordances on the three remaining buttons + the
matching server-side error-sanitisation hardening + the deploy
script's permission-stability preflight).

## User-facing change

- **Start / Stop / Delete cluster buttons** in the AKS cluster pulse
  are now disabled with the documented "you need X" tooltip for
  users without `Contributor` (or equivalent) on the cluster scope
  (critique #6 / continuation).
- **Run BLAST** button in both submit surfaces (mobile footer +
  desktop summary rail) is disabled with the tooltip for users
  without `Storage Blob Data Contributor + Contributor` at the
  cluster scope (#6).
- **ACR Build** button (both bulk and per-image) is disabled with
  the tooltip for users without `Contributor` at the ACR's RG
  (#6).
- **BLAST submit 4xx error bodies no longer leak SAS tokens, sig=
  params, or subscription ids** through `str(exc)` paths. Every
  exception message is now run through
  `api.services.sanitise.sanitise` before truncation (critique #7).
- **`scripts/dev/quick-deploy.sh`** now runs an ARM read-access
  preflight on the resource group / ACR / Container App before any
  destructive step, so a permission gap surfaces with a clear
  remediation message (with the exact `az role assignment list`
  command) instead of after a 30-90 s build (critique #8).

## API / IaC diff summary

### Backend (`api/`)

| File | Change |
|---|---|
| [api/routes/blast/submit.py](../../../api/routes/blast/submit.py) | New `_safe_exc_message(exc)` helper centralising sanitise + truncate; 4 `str(exc)[:_EXCEPTION_DETAIL_MAX_CHARS]` sites + the `summary[:…]` site replaced with sanitised equivalents (#7) |
| [api/tests/test_blast_submit_error_sanitisation.py](../../../api/tests/test_blast_submit_error_sanitisation.py) | New — 8 tests pinning the SAS / sig / subscription-id redaction contract + truncation cap (#7) |

### Frontend (`web/`)

| File | Change |
|---|---|
| [web/src/components/cards/ClusterPulse/PulseActions.tsx](../../../web/src/components/cards/ClusterPulse/PulseActions.tsx) | Wires `usePermissions(sub, rg, cluster)`; Start/Stop wrapped under `can_start_stop`, Delete under `can_delete`; `permissionDeniedTooltip` merged into each button's `title` and `disabled` (#6) |
| [web/src/pages/BlastSubmit.tsx](../../../web/src/pages/BlastSubmit.tsx) | Computes `submitPermissions` at the page level; `effectiveCanSubmit = validation.canSubmit && !submitPermissionDenied`; `handleSubmit` early-returns with a toast when denied (#6) |
| [web/src/pages/blastSubmit/BlastSubmitFooter.tsx](../../../web/src/pages/blastSubmit/BlastSubmitFooter.tsx) | New optional `permissionTooltip` prop overrides the generic run-title hint when present (#6) |
| [web/src/pages/blastSubmit/SubmitSummaryRail.tsx](../../../web/src/pages/blastSubmit/SubmitSummaryRail.tsx) | Same `permissionTooltip` plumbing for the desktop rail (#6) |
| [web/src/components/cards/AcrCard/AcrCard.tsx](../../../web/src/components/cards/AcrCard/AcrCard.tsx) | Wires `usePermissions(sub, acrRg)`; Build button now carries `disabled=buildDenied` + `title=permissionTooltip`; per-image `onBuildSingle` short-circuits when denied (#6) |

### Scripts

| File | Change |
|---|---|
| [scripts/dev/quick-deploy.sh](../../../scripts/dev/quick-deploy.sh) | New `preflight_permission_check()` runs after `confirm_deploy_target` for both the `all` and per-sidecar paths; probes `az group/acr/containerapp show` and fails fast with the exact role + `az role assignment list` command needed. Skippable via `ELB_QUICK_DEPLOY_SKIP_PREFLIGHT=1` for CI (#8) |

### IaC

No infra changes in this wave.

## Validation evidence

```text
$ uv run pytest -q api/tests
............................................................... [100%]
1906 passed, 3 skipped in 44.66s

$ cd web && npm test -- --run
 Test Files  56 passed (56)
      Tests  433 passed (433)

$ uv run ruff check api
All checks passed!

$ cd web && npm run build
✓ built in 7.98s

$ bash -n scripts/dev/quick-deploy.sh
(no output — syntax OK)
```

## Self-review

- Consumer search for `usePermissions` confirmed exactly four call
  sites land in this PR (AutoStopPanel from the prior commit +
  PulseActions + BlastSubmit + AcrCard). Each scopes the permission
  query to the smallest meaningful Azure scope (cluster for AKS
  actions; RG for ACR build).
- Consumer search for `str(exc)[:_EXCEPTION_DETAIL_MAX_CHARS]` is
  now zero in `api/routes/blast/submit.py`; the remaining instances
  in the workspace are all in other route files and out-of-scope
  for #7 (followup: roll the helper out to those).
- The `effectiveCanSubmit` derived flag is plumbed through both
  submit surfaces; `handleSubmit` carries a defence-in-depth early
  return so a keyboard / programmatic activation cannot slip
  through.
- `permissionDeniedTooltip` is the same import in all three new
  consumers, so a future change to the wording propagates
  automatically.
- `preflight_permission_check` uses `az ... show -o none` so a
  successful probe stays silent; failures `die` with both a clear
  reason and the exact diagnostic command.

## Followups (deliberately deferred)

- Wire `_safe_exc_message` (or a sibling helper) to the other
  `api/routes/blast/*` modules that still embed raw `str(exc)`
  (jobs.py, results.py, …) — out of scope this round.
- Add a `useEffect` in `BlastSubmit.tsx` to invalidate the
  permission query when the user picks a different cluster — the
  default `staleTime: 60s` plus the natural unmount/remount on
  navigation is acceptable for now.
- The AcrCard per-image build button stays visually enabled but
  no-ops when denied (the page-level Build button is the canonical
  affordance). A future round can promote the per-row buttons to
  the same disabled-with-tooltip treatment.
