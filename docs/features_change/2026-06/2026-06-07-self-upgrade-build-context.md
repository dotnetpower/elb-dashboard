---
title: Self-upgrade build — context cwd and terminal base image
description: Make az acr build run from inside the context dir with SOURCE_LOCATION="." and pass the ACR-qualified terminal base image, fixing the final two blockers in the self-upgrade build pipeline.
tags:
  - operate
  - deployment-reference
---

# Self-upgrade build — context cwd and terminal base image

## Motivation

After the earlier self-upgrade fixes (PLATFORM_ACR_NAME, failed_pre restart,
az login MSI, commit clone), the build still failed — first every component
with `az acr build … ERROR: Unable to find 'api/Dockerfile'.` (despite the file
being tracked and on disk), then the `terminal` component with
`pull access denied for elb-terminal-base`.

## Root causes (two final blockers)

1. **Absolute SOURCE_LOCATION + per-request temp cwd.** The build passed the
   cloned context as an **absolute** path while the exec call ran in a
   per-request temporary working directory. `az acr build` reported
   "Unable to find 'api/Dockerfile'" even though `git status --porcelain`
   (added as a diagnostic) reported the file present on disk. Running the build
   from **inside** the context with `SOURCE_LOCATION="."` resolved it — this is
   the standard `docker build .` shape.
2. **Terminal base image not registry-qualified.** `terminal/Dockerfile.runtime`
   is a thin overlay: `FROM ${TERMINAL_BASE_IMAGE}` whose default ARG is the
   bare `elb-terminal-base:latest`. An ACR build cannot pull a registry-less
   reference, so the terminal build failed with "pull access denied". The
   `quick-deploy.sh` path passes `--build-arg TERMINAL_BASE_IMAGE=<acr>/…`, but
   the self-upgrade image builder did not.

## User-facing change

Self-upgrade builds now complete the `api` and `frontend` images (verified live:
`building 30% az acr build api` → `building 43% az acr build frontend`) and the
`terminal` image pulls its base from ACR.

## API / IaC diff summary

- `api/services/upgrade/image_builder.py`:
  - `build()` runs `runner.stream(argv, cwd=source_dir, …)` and `_argv_for`
    emits `SOURCE_LOCATION="."` instead of an absolute path.
  - The `terminal` component gets
    `--build-arg TERMINAL_BASE_IMAGE=<acr>.azurecr.io/elb-terminal-base:latest`
    so the overlay can pull its toolchain base. (`latest` is kept current by
    every deploy of the base image.)
  - A pre-build diagnostic logs `git status --porcelain <dockerfile>` so a
    working-tree vs index gap is visible in the build log.
- `api/services/upgrade/git_workspace.py` (earlier commits in the chain):
  commit clone is a shallow `--depth 1 --no-checkout` clone + `git fetch
  --depth 1 origin <sha>` + detached checkout, mirroring the working release
  path.
- No infra change.

## Validation evidence

- Live: the upgrade reached `building` and progressed api → frontend, proving
  the context-cwd fix. Terminal then failed only on the base-image pull, now
  fixed by the registry-qualified build-arg.
- `uv run pytest -q api/tests` → all upgrade tests green, including new
  `test_build_terminal_passes_acr_base_image_arg`.
- `uv run ruff check` on touched files → clean.
