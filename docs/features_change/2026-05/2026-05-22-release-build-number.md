---
title: Release Build Number Stamp
date: 2026-05-22
area: frontend
---

## Motivation

Operators need the dashboard header to show both the human-managed release train
and the exact deployed build without committing a version-file change on every
push.

## User-Facing Change

The SPA header now renders the release version with a build number derived from
commits since the latest release tag, followed by the short commit SHA. For
example, a committed release version of `0.2.0` can display as
`v0.2.17 · 4060551` on the seventeenth post-release build. The tooltip shows
the release version, displayed build version, build number, commit, and build
timestamp.

## API / IaC Diff Summary

- Added `APP_BUILD_NUMBER` to the frontend Docker build arguments and Vite
  build-time constants.
- Updated `scripts/dev/quick-deploy.sh frontend` and `scripts/dev/postprovision.sh`
  to compute the build number on the host before `az acr build`.
- Updated the GitHub Release workflow tag trigger to match `v*.*.*` release
  tags reliably.
- Updated release-version documentation so `web/package.json` and
  `pyproject.toml` store `A.B.0`, while the displayed `C` value is build-time
  only.

## Validation Evidence

- `bash -n scripts/dev/bump-version.sh scripts/dev/quick-deploy.sh scripts/dev/postprovision.sh` passed.
- `scripts/dev/bump-version.sh --dry-run` reported `0.1.0 -> 0.2.0` with auto-detected `release` bump.
- `cd web && npm run build` passed; the generated main bundle contains the current `v0.1.1` build stamp and short SHA `4ff4e8e`.