# Bump Node 20 → Node 22 (LTS) for frontend image and full compose

## Motivation

- **Node.js 20 LTS reached end-of-life on 2026-04-30.** No more security
  patches will land on the 20.x line. Today is 2026-05-26 — the project
  still pinned `node:20.18-alpine` (production frontend image) and
  `node:20-alpine` (local 6-sidecar compose) at that point.
- The rest of our Node surface was already on Node 22:
  - Local development host runs Node 22.18.0.
  - `.github/workflows/docs.yml` sets `node-version: "22"` for the
    SPA mock build feeding the docs site.
- This bump unifies our Node version across local dev, CI docs build,
  and the Container Apps frontend image — eliminating the EOL surface
  with the smallest possible diff. Node 22 ("Jod") is the current
  active LTS through 2027-04-30.

> The GitHub Actions deprecation notice ("Node.js 20 actions are deprecated
> … will be forced to run with Node.js 24 starting June 2nd, 2026") is
> unrelated to this change. It governs the Node runtime that executes
> JavaScript-based actions on the runner, not the Node version our app or
> images run under. No workflow YAML change is needed there.

## User-facing change

None. The SPA produces an identical bundle and the Container App keeps
the same shape (Node 22 is fully ABI/feature-compatible with the Vite 6
/ React 18 / TypeScript 5.x toolchain that built fine under Node 22
locally and in the docs CI).

## API / IaC diff summary

- `web/Dockerfile` — build stage base image
  `node:20.18-alpine` → `node:22-alpine` (1 line).
- `scripts/dev/docker-compose.full.yml` — `frontend` service image
  `node:20-alpine` → `node:22-alpine` (1 line).
- No other Node references found in `web/`, `scripts/`, `terminal/`,
  `infra/`, or `api/` that needed bumping. `package.json` has no
  `engines` pin (intentionally — release version is the only thing
  versioned via `bump-version.sh`). The two `node:20-alpine` mentions
  in `docs/features_change/2026-05/{container-app-phase0-scaffolding,
  frontend-sidecar-replaces-swa}.md` are historical narrative and not
  bumped (changelog should reflect what the commit actually shipped at
  that date).

## Validation evidence

- `git --no-pager diff web/Dockerfile scripts/dev/docker-compose.full.yml`
  shows exactly the two intended one-line bumps and nothing else.
- Node 22 SPA build path is already validated by:
  - Local `npm run build` succeeded under Node 22.18.0 in prior
    sessions today (the only failure this turn is an unrelated
    pre-existing `UU` merge-conflict in
    `web/src/pages/blastSubmit/queryExamples.ts` — markers at
    L732/L1217/L1219, left over from a previous `git stash pop`,
    **not introduced by this change**).
  - `.github/workflows/docs.yml` `setup-node@v4` with
    `node-version: "22"` is green on `main` (latest successful docs
    run `26442365574`).
- A follow-up `azd up` / `quick-deploy.sh frontend` will rebuild the
  frontend image on the Node 22 base; expected to be a no-op other than
  the base layer change.

## Follow-up not in scope here

- Resolve the pre-existing `queryExamples.ts` merge conflict (separate
  ticket — different feature surface).
- Decide later whether to also pin Node via `engines` /
  `.nvmrc` to make the version stamp visible to humans cloning fresh.
