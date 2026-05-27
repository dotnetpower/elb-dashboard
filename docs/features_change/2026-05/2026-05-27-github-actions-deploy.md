# GitHub Actions deploy: build on push, deploy on demand

## Motivation
Until now every deploy to the bundled Container App ran from a maintainer's
laptop via `scripts/dev/quick-deploy.sh` or `scripts/dev/postprovision.sh`.
The user asked for the build step to run automatically in CI while keeping
the actual revision swap behind a manual trigger they control from the
GitHub UI.

## User-facing change
Two new workflows under `.github/workflows/`:

- **Build Images** (`build-images.yml`) — runs on every push to `main`
  that touches `api/**`, `web/**`, `terminal/**`, or the related shell
  helpers under `scripts/dev/`. Builds `elb-api`, `elb-frontend`, and
  `elb-terminal` in parallel via `az acr build` and tags each as
  `gha-<short-sha>` plus `latest-main`. Does **not** touch the running
  Container App.
- **Deploy to Container App** (`deploy.yml`) — `workflow_dispatch` only.
  Maintainer picks a sidecar (`all` / `api` / `worker` / `beat` /
  `frontend` / `terminal`) and a tag (defaults to `latest-main`).
  Verifies the chosen tag exists in ACR, then patches the Container App
  via `az containerapp update`. Gated behind a GitHub `production`
  environment that requires a maintainer approval click before
  `az containerapp update` actually runs. Ends with a 3-minute retry
  loop on `https://$CONTAINER_APP_FQDN/api/health`.

## API / IaC diff summary
- `scripts/dev/quick-deploy.sh`:
  - new `--no-build` flag: skip `az acr build`, only PATCH an existing tag.
    Used by `deploy.yml`.
  - new `--build-only` flag: build images, skip the Container App PATCH.
    Used by `build-images.yml`.
  - The two flags are mutually exclusive (`die` if both set).
  - Frontend PATCH conditionally skips `--set-env-vars` when `--no-build`
    so GHA does not need to re-resolve VITE_* on every deploy — runtime
    env from the last full deploy (or Bicep) stays as-is, while the
    build-baked VITE_* values from the image remain authoritative.
- `scripts/dev/setup-gha-oidc.sh` (new) — one-shot, idempotent helper that
  creates an App Registration `gha-elb-dashboard`, federated identity
  credentials for `main` push / PR / `production` environment, and the
  minimum RBAC: `AcrPush` on the ACR, `Contributor` on the Container App,
  `Reader` on the RG. No client secrets. Prints the GitHub secrets +
  variables to paste into repo settings.
- No Bicep changes. No new Azure resources beyond the App Registration.

## Security
- OIDC federated credential only — no client secret in source, env, or
  Key Vault. Satisfies charter §12.
- Scope is intentionally narrow: `AcrPush` on the single ACR, `Contributor`
  only on the Container App (not the whole RG). The principal cannot
  create/delete resources, cannot read Key Vault, cannot touch Storage.
- `production` environment with required reviewer means an attacker who
  somehow forces a workflow run still cannot mutate the live revision
  without a human approval click.

## Validation evidence
- `bash -n scripts/dev/quick-deploy.sh` → syntax OK after both patches.
- `bash -n scripts/dev/setup-gha-oidc.sh` → syntax OK.
- Workflow YAML follows the same patterns as the existing
  `.github/workflows/{docs,release,test}.yml` files.
- The workflows have not been exercised end-to-end yet — the operator must
  first run `scripts/dev/setup-gha-oidc.sh`, paste the secrets/variables
  into GitHub, and create the `production` environment. The first push
  to `main` after that will produce the first artifact.

## Rollback
- Delete `.github/workflows/build-images.yml` and `.github/workflows/deploy.yml`.
- Revert the `quick-deploy.sh` patch — the `--no-build` / `--build-only`
  paths are additive and the default behaviour is unchanged.
- `scripts/dev/setup-gha-oidc.sh` is opt-in. If the OIDC App Registration
  has already been created, `az ad app delete --id <APP_ID>` removes it.
