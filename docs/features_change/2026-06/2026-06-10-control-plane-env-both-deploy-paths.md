# Control-plane guard env applied on both full and fast deploys

## Motivation

The dashboard entry RBAC gate (`ENFORCE_DASHBOARD_RBAC`) was flipped to `true`
in Bicep and tests were green, but after a redeploy a no-RBAC user could still
load the full dashboard. Root cause: `scripts/dev/quick-deploy.sh` — used by
BOTH local fast deploys AND the GitHub Actions `deploy.yml` path — patches
container **images only** and never touches env vars. Container App env values
land exclusively through a full `azd provision` / postprovision Bicep deploy, so
a guard-default change in Bicep silently never reached either fast path. The
live api container had no `ENFORCE_DASHBOARD_RBAC` env at all (also missing
`STRICT_*` / `ENFORCE_*` guards), proving the guard envs had never been
provisioned end-to-end after they were added to Bicep.

## User-facing change

No UI change. Deployment behaviour change: a control-plane guard/policy env
default set in the repo now applies consistently across **all** deploy methods —
`azd provision`, postprovision Bicep redeploy, local `quick-deploy.sh`, and the
GitHub Actions deploy workflow.

## API / IaC diff summary

- **`infra/control-plane-env.json`** (new): single source of truth for the
  Container App guard/policy toggles, keyed per sidecar (`api` / `worker` /
  `beat`). Values are strings (`OPENAPI_ALLOW_PUBLIC_LB`,
  `ENFORCE_OPENAPI_EXEC_RBAC`, `ENFORCE_DASHBOARD_RBAC`, `BLAST_GATE_ENABLED`,
  `BLAST_JOBS_SHARED_VISIBILITY`, `STRICT_BLUEGREEN`).
- **`infra/modules/containerAppControl.bicep`**: adds
  `var controlPlaneEnv = loadJsonContent('../control-plane-env.json')` and
  replaces the six api + two worker + two beat literal guard values with
  `controlPlaneEnv.<sidecar>.<KEY>` references. Inline documentation comments are
  preserved; only the values now come from the JSON. Compiles cleanly
  (`az bicep build`), embedding `ENFORCE_DASHBOARD_RBAC: "true"`.
- **`scripts/dev/quick-deploy.sh`**: reads the same JSON and upserts the
  per-sidecar guard toggles as `--set-env-vars` on every api/worker/beat PATCH
  (both the parallel `all` path and the single-sidecar path; `--no-build`
  included, which is the GHA path). `--set-env-vars` is an upsert — it only
  touches the listed keys, leaving image/secret/other env intact. Frontend keeps
  its existing VITE_* sync; terminal/redis have no guard toggles (no-op).
  Malformed JSON aborts the deploy; a missing file degrades to image-only.
- **`api/tests/test_control_plane_env.py`** (new): asserts the JSON parses, the
  expected sidecars/keys exist, all values are strings, `ENFORCE_DASHBOARD_RBAC`
  stays `"true"`, every JSON key is referenced in the Bicep (`controlPlaneEnv.…`
  cross-check so a rename in one place fails CI), and no secret-backed key
  (e.g. `EXEC_TOKEN`) leaks into the literal env JSON.

## Why this is the right boundary

- `infra/main.json` is a build artifact — `azd provision` recompiles
  `infra/main.bicep` and postprovision runs `az deployment group create
  --template-file containerAppControl.bicep`, both of which compile the Bicep
  (and resolve `loadJsonContent`) at deploy time. No manual recompile needed.
- Only literal policy toggles moved to the JSON. Param-derived env (endpoints,
  tenant id, secrets via `secretRef`) stays inline in Bicep, since quick-deploy
  cannot and must not re-derive or expose those.

## Persona impact (charter 12a)

No persona regression. This change does not narrow any RBAC role; it makes an
already-decided guard default actually deploy. The dashboard gate itself
degrades OPEN if the shared MI cannot read role assignments, so it can never
lock out a legitimate operator. Persona Matrix unaffected (no route gate
changed, no SSE change).

## Validation evidence

- `az bicep build --file infra/modules/containerAppControl.bicep` → compiles;
  compiled output embeds `"ENFORCE_DASHBOARD_RBAC": "true"`.
- `bash -n scripts/dev/quick-deploy.sh` → syntax OK; helper emits the correct
  per-sidecar pairs (api 6, worker 2, beat 2, terminal 0).
- `uv run pytest -q api/tests` → 3194 passed, 3 skipped.
- `uv run ruff check api` → clean.
- Live remediation (separate from this code change): `az containerapp update
  --container-name api --set-env-vars ENFORCE_DASHBOARD_RBAC=true` already
  applied the gate on revision `ca-elb-dashboard--0000322`; this change makes
  every future deploy keep it applied automatically.

## Hardening discipline (§12a)

- [x] In scope: rbac (deploy-path wiring of the `ENFORCE_DASHBOARD_RBAC` guard)
- [x] RBAC change is single-PR safe (no role narrowed) — additive deploy wiring
      only; no `roleAssignments` resource added or removed.
- [x] Persona Matrix tests pass for owner / contributor / reader / dev_bypass
      (`api/tests/test_persona_matrix.py` unchanged + green).
- [x] Reader allowlist unchanged.
- [x] Capability Probe unaffected (no new role introduced).
- [x] RBAC removal preflight N/A — no Bicep `roleAssignments` deletion in this
      change (`scripts/dev/check_rbac_removal.py` surface untouched).
- [x] New guard ships behind an env toggle — `ENFORCE_DASHBOARD_RBAC` already
      existed (default-OFF originally); this change only makes its repo value
      deploy consistently. `infra/control-plane-env.json` is the toggle source.
- [x] No `Depends(require_caller)` added to an SSE event stream.
- [x] Change note (this file) summarises persona impact above.

