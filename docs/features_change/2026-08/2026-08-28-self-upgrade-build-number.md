---
title: Self-upgrade build number parity
description: Preserve the release-relative SPA build number when the in-app commit channel builds and deploys a new frontend image.
tags: [ui, release, operate]
---

# Self-upgrade build number parity

## Motivation

A successful in-app commit upgrade deployed the correct commit `ae493a5`, but the new SPA header rendered `v0.3.0` and About rendered `v0.3.0 · ae493a5`. The standard quick-deploy and postprovision paths pass `APP_BUILD_NUMBER`; the self-upgrade image builder passed only the release and commit, so the frontend Docker build fell back to build number zero.

## User-facing change

Frontends produced by the in-app commit upgrade now show the same `vA.B.<commits-since-release> · <short-sha>` identity as standard deployments. The header, Settings footer, Upgrade page, and About panel therefore agree after self-upgrade.

## API and infrastructure summary

- Commit checkouts complete their shallow git history and tags before the remote URL credential scrub, then verify the matching release tag is an ancestor of the target.
- The workspace records the numeric `vA.B.0..HEAD` commit count. The image builder validates it and passes it only to the frontend as `APP_BUILD_NUMBER`.
- Release-tag upgrades retain the default build number zero. Commit image builds must supply the verified number, preventing another caller from silently falling back to zero.
- History/tag fetches are bounded by the existing 300-second git operation timeout. Missing tags, invalid ancestry, or non-numeric output fail before any Container App traffic patch.
- Git command failures mask URL userinfo before error text reaches the durable upgrade state or browser.
- No authentication, role assignment, network, Storage, or sidecar topology changed.

## Validation

- Real read-only shallow clone of target `ae493a5`: pre-fetch count `1` with no `v0.3.0` tag; post-unshallow build number `35` with the tag present.
- Focused git-workspace, image-builder, and upgrade-pipeline tests: 81 passed; Persona Matrix remained green.
- The pre-fix live deployment reproduced header `v0.3.0` / About `v0.3.0 · ae493a5`, proving the missing Docker build argument.
- Full backend, frontend, documentation, and post-deploy browser verification is recorded with the remediation commit.
