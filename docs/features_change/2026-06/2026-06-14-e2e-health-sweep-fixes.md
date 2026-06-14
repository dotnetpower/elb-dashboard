---
title: E2E health sweep — MessageFlow halo stability, lint, and local web launcher
description: Post-update full validation sweep that fixed an unstable MessageFlow job-node click target, an eslint warning that failed the web lint gate, and a hanging azd call in the local web launcher.
tags:
  - ui
  - contributor
---

# E2E health sweep — MessageFlow halo stability, lint, and local web launcher

## Motivation

A large code update (MessageFlow constellation, energy particles, BLAST failure
detail surfacing, auto-warmup toast, CI serialization) landed across backend,
frontend, and dev tooling. A full `local-safe` validation sweep was run to
confirm overall health and fix anything red.

The sweep surfaced three real defects (one user-visible, two developer-facing).

## User-facing change

- **MessageFlow constellation — running/reducing job nodes are now click-stable.**
  The breathing halo (`.mf-halo`) around an in-flight job previously animated
  `transform: scale(1 → 1.35)`. Because the halo is a child of the interactive
  `.mf-node` `<g>`, the group's bounding box resized on every animation frame,
  so the job node was never geometrically "stable" — pointer actionability
  (e.g. Playwright clicks) timed out and the live hit region subtly reflowed.
  The pulse now animates **opacity only** (`0.08 → 0.32`), keeping the halo's
  geometry fixed. The breathing energy still reads, and `prefers-reduced-motion`
  already fell back to a static opacity, so that path is unchanged.

## Developer-facing changes

- **`useStickToBottom` eslint gate fixed.** A recent edit added an
  `isFollowing()` call inside a `useEffect` keyed only on `[enabled]`, producing
  a `react-hooks/exhaustive-deps` warning that failed `eslint --max-warnings 0`.
  Fixed by reading the latest `isFollowing` through a ref (`isFollowingRef`),
  matching the file's existing `requestScrollRef` pattern — no re-subscription,
  no behaviour change.
- **Local web launcher no longer hangs on `azd`.** `scripts/dev/local-run.sh web`
  called `azd env get-values` without the `timeout` / `</dev/null` guard every
  other call site uses. In a non-TTY context a prompting/slow `azd` blocked the
  Vite dev server past the e2e readiness window, breaking
  `scripts/dev/e2e-ui.sh --fullstack`. Now guarded exactly like
  `lib-env.sh::load_azd_env` (the client id is optional under dev-bypass, so
  failing fast is always safe).

## API / IaC diff summary

No API, schema, or IaC changes. CSS, one React hook, and one dev script only.

## Validation evidence

- Backend: `uv run ruff check api` clean; `uv run pytest -q api/tests` → 3503 passed, 3 skipped.
- Frontend: `npm test -- --run` → 859 passed; `npm run lint` → 0 warnings; `npm run build` → ok.
- Docs: `scripts/docs/check_frontmatter.py` → 55 pages ok.
- E2E (`scripts/dev/e2e-ui.sh bypass --headless --fullstack`, `e2e:all-safe`):
  before fix → MessageFlow `redacted job detail` test failed (element not stable);
  after fix → 28 passed, 1 skipped. The targeted test
  `message-flow-events.ui.spec.ts:144` passes in 4.3s.
- MessageFlow unit suite (`layout`, `constellationModel`) → 30 passed.
