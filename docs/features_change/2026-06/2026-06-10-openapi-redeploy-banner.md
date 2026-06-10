# elb-openapi redeploy banner (manifest drift detection)

## Motivation

Manifest-only changes to the `elb-openapi` Deployment (e.g. the single-replica
queue owner in the sibling change note `2026-06-10-openapi-single-queue-owner`)
do not take effect until someone re-runs **Deploy elb-openapi** — Bicep/azd
never touch that in-cluster Deployment. There was no signal in the dashboard
telling the user a redeploy was required, so a live cluster could keep running
the old (two-replica) manifest indefinitely after the dashboard image shipped
the fix.

## User-facing change

The API Reference page now shows a redeploy banner when the live `elb-openapi`
Deployment's manifest predates the dashboard's current generation. The banner
reuses the existing OpenAPI deploy panel (`variant="update"`,
`reason="manifest"`) and explains that the redeploy applies the latest
configuration (single queue owner so the `/v1/jobs` concurrency limit is
enforced). When the pinned image tag ALSO changed, only the image-update panel
shows (its redeploy re-applies the manifest too), avoiding two stacked panels.

## API / IaC diff summary

- `api/tasks/openapi/constants.py`: new `OPENAPI_MANIFEST_REVISION` (=2) and
  `OPENAPI_MANIFEST_REVISION_ANNOTATION` (`elb-dashboard/manifest-revision`).
  Bump the revision by 1 whenever a `manifests.py` change must be redeployed to
  take effect.
- `api/tasks/openapi/manifests.py`: stamp the revision as a Deployment metadata
  annotation in `build_manifests`.
- `api/services/openapi/deployment.py`: read the live annotation; add
  `manifest_revision`, `expected_manifest_revision`, and `manifest_outdated`
  (true when the annotation is missing or lower than the shipped revision) to
  the `/api/aks/openapi/deployment` response.
- `web/src/api/aks.ts`: extend `OpenApiDeploymentStatus` with the three fields.
- `web/src/components/OpenApiDeployPanel/{OpenApiDeployPanel,DeployHeader}.tsx`:
  new `reason: "image" | "manifest"` prop with manifest-specific copy.
- `web/src/pages/ApiReference.tsx`: render the panel when image OR manifest is
  outdated; prefer the image reason when both, to avoid stacking.
- `web/src/mocks/docsPreview.ts`: add the three fields to the mock response.

No Bicep / Container App changes.

## Validation evidence

- `uv run pytest -q api/tests/test_openapi_task.py
  api/tests/test_openapi_deployment.py` — 16 passed (annotation stamped;
  missing/lower revision → outdated; equal → current).
- `uv run pytest -q api/tests -k "openapi or manifest or deployment or
  external_blast"` — 308 passed.
- `uv run ruff check` (changed backend files) — passed.
- `cd web && npm run build` — type-check + build passed.
- `cd web && npm test -- --run` — 768 passed.
