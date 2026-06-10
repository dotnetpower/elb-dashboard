# elb-openapi redeploy banner — surface silent-failure cases

## Motivation

The "Redeploy OpenAPI service" prompt on the API Reference page could vanish
without explanation, leaving an operator who "already deployed" unsure why no
banner appears. Two cases failed closed:

1. **Deployment read error** — when the workload-cluster kubectl read fails
   (cluster unreachable / missing RBAC), `deploymentQuery.data` is undefined, so
   `manifest_outdated` evaluates to `false` and the banner silently disappears.
2. **Control-plane (api image) predates drift detection** — an older `api`
   image returns NO `manifest_outdated` field at all (vs. an explicit `false`
   from a current api). The redeploy prompt then never fires because the signal
   the banner depends on does not exist yet — the real fix is to redeploy the
   control plane, but nothing said so.

This is the most likely reason the banner "doesn't show after a deploy": a
frontend-only / partial deploy leaves the api image old, so the manifest-drift
signal is simply absent.

## User-facing change

When the redeploy panel does not apply, the page now distinguishes:

- **Read failed** → a warning panel: "elb-openapi deployment status
  unavailable" with the cluster-access cause and a Refresh button.
- **Signal missing** (old api image) → a warning panel: "Redeploy detection not
  available yet — redeploy the control plane (rebuild + roll the api image) to
  enable the redeploy prompt."

The existing image-tag / manifest-outdated redeploy panel is unchanged; these
diagnostics only fill the gap where it previously rendered nothing.

## API / IaC diff summary

- `web/src/pages/ApiReference.tsx`: the banner gate now branches on
  `deploymentReadFailed` (`deploymentQuery.isError`) and `manifestSignalMissing`
  (`deploymentQuery.data.manifest_outdated === undefined`), rendering a new
  local `OpenApiManifestDiagnostic` component instead of returning `null`.

No backend / IaC changes. `OpenApiDeploymentStatus.manifest_outdated` is already
optional, so the `=== undefined` discriminator is type-safe.

## Validation evidence

- `cd web && npm run build` — type-check + build passed.
- `cd web && npm test -- --run` — 777 passed (no regressions).

## Note on why the banner needs a redeployed api image

`/api/aks/openapi/deployment` only returns `manifest_revision` /
`manifest_outdated` from the api image that shipped manifest-drift detection.
If the deployed environment's banner is missing, check the route response:
`manifest_outdated` absent ⇒ redeploy the control plane; `manifest_revision: 2`
⇒ elb-openapi is already current (no banner is correct); a 5xx ⇒ the read-failed
diagnostic now surfaces it.
