# ACR Build: Switch from VM Run Command to ACR Build Tasks API

**Date**: 2026-05-08

## Motivation

The previous implementation of `build_acr_images` used VM Run Command to
execute `az acr build` on the terminal VM. This required the VM to have an
active `az login` session, which frequently failed with
`"Please run 'az login' to setup account"`.

## User-Facing Change

- **Build Images** button on the ACR card now works without a terminal VM
  or `az login` on the VM.
- Builds are scheduled via the Azure Container Registry Build Tasks API
  (`begin_schedule_run`) using the backend's credential directly.
- Source code is pulled from GitHub (`dotnetpower/elastic-blast-azure`,
  `master` branch) — no local clone or Docker daemon required.
- Per-image build errors are displayed individually in the ACR card with
  log excerpts.

## API / Code Changes

| File | Change |
|------|--------|
| `api/services/image_tags.py` | Replaced `dir`/`pre_cmd`/`post_cmd` with `context`/`dockerfile`. Added `SOURCE_REPO` and `SOURCE_BRANCH` constants. |
| `api/function_app.py` (`build_acr_images`) | Replaced `ComputeManagementClient` + VM Run Command with `ContainerRegistryManagementClient` + `begin_schedule_run` using `DockerBuildRequest`. Polls run status until terminal. Fetches build logs on failure. |
| `web/src/api/endpoints.ts` | Removed `vmRg`/`vmName` params from `buildAcrImages`. |
| `web/src/components/cards/AcrCard.tsx` | Added per-image error display; updated building message to mention ACR Build Tasks. |

## Validation

- Manual test: `ncbi/elb:1.4.0` built successfully via `begin_schedule_run`
  (Run ID: de2, Succeeded after 2m12s).
- Python syntax verified: `py_compile` passes for both changed files.
- TypeScript: no new type errors from AcrCard or endpoints changes.
