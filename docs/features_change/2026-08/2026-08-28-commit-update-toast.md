---
title: Commit update check notification
description: Report commit-channel updates accurately after an operator checks the remote from the self-upgrade page.
tags: [ui, release]
---

# Commit update check notification

## Motivation

The self-upgrade page correctly showed that a newer `main` commit was available, but the notification emitted after **Check remote** compared only release versions. When the release remained `0.3.0`, the notification incorrectly said the deployment was already on the latest build even though a newer commit target was selectable.

## User-facing change

**Check remote** now distinguishes release updates from commit-channel updates. A same-release commit update reports the latest short commit identifier as an available upgrade instead of claiming that the running build is current.

## API and infrastructure summary

No API, authorization, deployment state machine, role assignment, network, Storage, or infrastructure behavior changed. The existing `latest_commit_sha` response field and commit comparison helper are reused.

## Validation

- Mocked upgrade mutation E2E: 6 passed, including a same-release newer-commit notification assertion.
- Full frontend validation: 109 files and 985 tests passed; ESLint and the production build passed.
- Safe full-stack Playwright validation: 56 passed and 6 explicitly guarded live-mutation scenarios skipped.
- Documentation frontmatter and strict MkDocs builds passed.
