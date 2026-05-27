# BLAST pre-flight: ACR images gate + one-click build remediation

## Motivation

A fresh `azd up` provisions the ACR but does **not** populate it — the four
BLAST runtime images (`ncbi/elb`, `ncbi/elasticblast-job-submit`,
`ncbi/elasticblast-query-split`, `elb-openapi`) only land after
[scripts/dev/postprovision.sh](../../../scripts/dev/postprovision.sh) or the
`/api/acr/build-images` task runs. When the user submitted BLAST against an
empty ACR the job sat in `queued` indefinitely, because the AKS pod hit
`ImagePullBackOff` and there was no signal in the SPA pointing at the root
cause.

## User-facing change

* `POST /api/blast/pre-flight` now surfaces an `acr_images` row:
  * `status=pass` — every required image resolves in the configured ACR.
  * `status=fail` + `action_type=build_acr_images` — at least one image is
    missing; the row carries `action_params={subscription_id, resource_group,
    registry_name}` so the SPA can call `/api/acr/build-images` directly.
  * `status=warn` — `acr_name` not provided by the form (non-blocking) or the
    ACR could not be reached (RBAC / network — non-blocking, override-able).
* `POST /api/blast/submit` enforces the same gate. When images are missing
  the route returns `409 blocked_by_preflight` with `error_code=acr_images_missing`
  *before* a job row is written, so the user never sees a stranded `queued` job.
* The Pre-Flight panel renders a "Build ACR images →" button on the new row.
  Click → queues the build task → button switches to
  "Build queued. View progress on Dashboard →" (deep-links to the ACR card
  via `/#acr-card`, which auto-scrolls into view). No automatic build on
  dashboard load (avoids surprise ACR charges and concurrent-user collisions).

## API / IaC diff summary

* `api/services/blast/submit_gates.py` — new `_gate_acr_images(acr_name)` (5 s
  cached per ACR), wired into `evaluate_submit_gates(..., acr_name="")`.
* `api/routes/blast/submit.py` — passes `acr_name` from the normalised body
  into the gate evaluator.
* `api/routes/blast/preflight.py` — surfaces the gate as a `checks[]` row
  with `action_params` so the SPA can call `/api/acr/build-images` without
  re-deriving sub/RG/registry.
* `web/src/pages/blastSubmit/PreFlightResultPanel.tsx` — new `BuildAcrButton`
  using `monitoringApi.buildAcrImages`. No new dependencies.
* `web/src/hooks/useScrollToHash.ts` — tiny shared helper that scrolls to
  `window.location.hash` on mount, so the Dashboard deep-link from the
  Pre-Flight build button lands on the ACR card without manual scrolling.
* `web/src/pages/Dashboard/{Dashboard,DashboardGrid}.tsx` — `Dashboard`
  consumes the hook; `DashboardGrid` wraps `AcrCard` in `<div id="acr-card">`
  so the `/#acr-card` link target exists.
* No Bicep / infra changes.

## Validation

* `uv run pytest -q api/tests/test_blast_submit_gates.py` → 20/20 pass
  (added `test_acr_images_gate_unknown_when_acr_name_empty`,
  `..._ok_when_all_present`, `..._fail_when_some_missing`,
  `..._unknown_when_lookup_blows_up`).
* `uv run pytest -q api/tests/ -k "preflight or pre_flight or blast_submit or response_contracts"` → 75/75 pass.
* `uv run pytest -q api/tests/` → 1617/1618 pass; the single flaky failure
  (`test_readiness_storage_probe_is_single_flight_on_cold_cache`) is a known
  thread-timing flake unrelated to this change and passes on re-run.
* `uv run ruff check api/services/blast/submit_gates.py api/routes/blast/preflight.py api/routes/blast/submit.py api/tests/test_blast_submit_gates.py` → clean.
* `cd web && npm run build` → clean (no new tsc errors).

## Rejected alternative: auto-build on dashboard load

Considered triggering `/api/acr/build-images` automatically the first time the
dashboard renders. Rejected because:

1. ACR build is 5–10 min and **billable** — a casual monitoring visitor
   shouldn't kick it off.
2. Multiple concurrent visitors would race the same build (idempotency would
   need extra plumbing).
3. Violates the charter principle ("the user must never be surprised by an
   action they did not initiate"; .github/copilot-instructions.md §0).

The `azd up` flow already runs `postprovision.sh` which builds images at
deploy time; the runtime gate covers the case where that step was skipped or
new image tags landed since the last deploy.
