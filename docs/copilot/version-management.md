---
title: Version Management
description: How the ElasticBLAST Control Plane release-version policy is enforced — scripts/dev/bump-version.sh, release train bumps, build numbers, and the SPA header version stamp.
tags:
  - agent
---

# Version Management

> Re-verified 2026-09-16 against `bump-version.sh`, Vite, and the repository's
> current-branch/no-push workflow.

The control plane carries a small release + build stamp in the SPA header
(`vA.B.<build> · <short-sha>`). The release version is bumped by a single script that
reads Conventional Commits since the last tag — no manual edits to
`package.json` or `pyproject.toml`. The build number is not committed; it is
computed at build time from commits since the latest release tag.

---

## 1. Version Policy

| Segment | Trigger | Decided by |
|---------|---------|------------|
| **A** | breaking product generation change you decide to ship | manual (`--major` flag) |
| **B** | release train bump when any `feat:` / `fix:` commit is ready to ship | auto, or manual with `--release` / `--minor` |
| **C/build** | commit count since the latest `vA.B.0` release tag | build-time only |

If a `BREAKING CHANGE` footer or `feat!:` / `fix!:` marker is detected in
the range, the script refuses to auto-bump and exits with code 2 — pass
`--major` to acknowledge. This prevents accidental `A` bumps from being hidden
behind a routine commit message.

`chore:`, `docs:`, `test:`, `refactor:`, `style:`, `build:`, `ci:` commits
do **not** trigger a bump. If nothing in the range warrants a bump, the
script exits 0 with `nothing to bump`. Force with `--release` / `--minor` /
`--major` only when you have a reason that doesn't map to a commit type.

`web/package.json` is the source of truth and stores release versions as
`A.B.0`. `pyproject.toml` is kept in sync by the same script so the backend
image carries the same release identifier. The displayed `C` value comes from
`APP_BUILD_NUMBER`, not from either file.

---

## 2. Header stamp pipeline

```
┌─────────────────────────────────────────────────────────────┐
│  quick-deploy.sh frontend / postprovision.sh                │
│   ├─ APP_VERSION      = node -p require('web/package.json') │
│   ├─ APP_BUILD_NUMBER = git rev-list --count <tag>..HEAD    │
│   ├─ GIT_COMMIT       = git rev-parse --short HEAD          │
│   └─ BUILD_TIME       = date -u +%Y-%m-%dT%H:%M:%SZ         │
│         │                                                   │
│         ▼ --build-arg                                       │
│  az acr build (web/Dockerfile)                              │
│   ├─ ARG APP_VERSION / APP_BUILD_NUMBER / GIT_COMMIT / ...  │
│   └─ ENV APP_VERSION=... APP_BUILD_NUMBER=...               │
│         │                                                   │
│         ▼ npm run build                                     │
│  web/vite.config.ts  define:                                │
│   ├─ __APP_VERSION__      = process.env.APP_VERSION ?? pkg  │
│   ├─ __APP_BUILD_NUMBER__ = env ?? count from latest tag    │
│   ├─ __APP_COMMIT__       = process.env.GIT_COMMIT ?? git   │
│   └─ __APP_BUILD_TIME__   = env ?? now()                    │
│         │                                                   │
│         ▼ baked into dist/assets/index-*.js                 │
│  web/src/components/Layout.tsx → `vA.B.<build> · <sha>`     │
└─────────────────────────────────────────────────────────────┘
```

**Why host-side resolution is required.** ACR build context excludes
`.git` (and even with it, the build container's WORKDIR is `/web`, not
the repo root). The vite `define` falls back to `git rev-parse` for
local `npm run dev` and `npm run build`, but production builds must pass
the values through `--build-arg`. Both `quick-deploy.sh frontend` and
`postprovision.sh` resolve them on the host. The Dockerfile already declares
those `ARG`s so a missing build-arg shows up as empty string, not as a build
failure.

**Build number rule.** `APP_BUILD_NUMBER` is the commit count from the latest
merged `vA.B.0` tag to `HEAD`. On the exact release tag this is `0`, so a
release stamped `0.2.0` displays as `v0.2.0`. The first post-release commit
displays as `v0.2.1`, the next as `v0.2.2`, and so on, while the committed
release version remains `0.2.0` until the next release bump.

**Where the values surface in the UI.** The caption is rendered next to
"Control Plane" in [web/src/components/Layout.tsx](../../web/src/components/Layout.tsx)
using the injected globals declared in
[web/src/vite-env.d.ts](../../web/src/vite-env.d.ts). The native
`title=` tooltip carries the release, displayed build version, build number,
commit, and build timestamp.

---

## 3. Bumping the version

Agent operating procedure:

- For any task that adds code, updates behaviour, or fixes a bug, evaluate the release impact before final handoff and state one recommendation: `major`, `minor/release`, or `no release bump`.
- Do not run the non-dry-run bump command automatically after ordinary implementation. Ask the maintainer to approve the exact bump path first, then run the script yourself once approved.
- When asked for the current version, read [web/package.json](../../web/package.json) and [pyproject.toml](../../pyproject.toml), verify they match, and include the latest release tag / build-number meaning when useful.
- When asked to bump the version, run `scripts/dev/bump-version.sh --dry-run`, summarize what it would do, recommend the exact follow-up command, and wait for approval before running it.
- If asked for a `patch` bump, explain that `C` is computed at build time and `--patch` is intentionally rejected. For a shipped fix under this policy, use the next `minor/release` bump unless the change is breaking and needs `--major`.

```bash
# Dry-run first to see what would happen.
scripts/dev/bump-version.sh --dry-run

# Auto (feat/fix → next release train).
scripts/dev/bump-version.sh

# Manual override.
scripts/dev/bump-version.sh --major
scripts/dev/bump-version.sh --release
scripts/dev/bump-version.sh --minor

# Push the release commit + tag.
git push origin "$(git rev-parse --abbrev-ref HEAD)" --follow-tags
```

The script:

1. Reads the current version from `web/package.json` (source of truth).
2. Scans commits in `<last-v-tag>..HEAD` (or full history if no tag exists).
3. Decides the bump kind from Conventional Commits: `feat:` and `fix:` both
   move to the next `B` release; `BREAKING CHANGE` requires `--major`.
4. Rewrites `web/package.json` (via `node`) and the first
   `version = "…"` line in [pyproject.toml](../../pyproject.toml), refreshes
   `uv.lock`, and generates the tagged/Unreleased documentation pages.
5. Updates the Releases index and MkDocs navigation for the new tag.
6. Creates `chore(release): vA.B.0` commit + annotated `vA.B.0` tag.
7. Does **not** push — that stays an explicit step so the maintainer can
   review the diff first.

---

## 4. Release workflow

Routine — finished commits are ready to release on the current branch:

1. Confirm the current branch contains the reviewed Conventional Commits and a
   clean version/release-note working tree.
2. `scripts/dev/bump-version.sh --dry-run` → confirm the bump kind.
3. `scripts/dev/bump-version.sh` → creates commit + tag locally.
4. Inspect: `git show HEAD` (release commit) and `git show vA.B.0` (annotated tag).
5. The maintainer runs `git push origin <current-branch> --follow-tags`; cloud picks up the new tag for
   GitHub releases automation.
6. Deploy the frontend with the new build stamp visible in the header:
   ```bash
   bash scripts/dev/quick-deploy.sh frontend
   ```
7. Open the cloud URL, hover the header version caption, confirm the
   displayed build number and commit short SHA match the deployed `HEAD`.

Hotfixes follow the same current-branch procedure. This repository's agent
workflow does not create a branch or push automatically; branch choice and
publication remain maintainer decisions.

---

## 5. Validation checklist

Before pushing a release tag:

- [ ] `scripts/dev/bump-version.sh --dry-run` shows the expected bump kind.
- [ ] `web/package.json` and `pyproject.toml` versions match (`git diff HEAD~1`).
- [ ] `cd web && npm run build` succeeds and `grep -oE '"<short-sha>"' dist/assets/index-*.js` returns the new SHA.
- [ ] `grep -oE '"[0-9]+"' dist/assets/index-*.js` or the header tooltip confirms the expected build number.
- [ ] Header tooltip shows the expected release version, displayed build version, build number, commit, and timestamp after page reload.
- [ ] No other unrelated diffs in the release commit (the script refuses if
      `package.json`/`pyproject.toml` already have unstaged edits).

---

## 6. Common mistakes

1. **Editing `package.json` version by hand.** The script will still run,
   but the commit history won't have the `chore(release): vA.B.0` marker
   that ties the tag to a known author intent. Always go through the
   script.
2. **Pushing without `--follow-tags`.** The release commit lands but the
   tag stays local — production never sees the new version. Use
   `git push origin <branch> --follow-tags`.
3. **Deploying frontend without re-running `quick-deploy.sh`.** A pure
   `azd provision` doesn't bake the new version+SHA — only the
   `--build-arg` path through `quick-deploy.sh` (or a refreshed
   postprovision build) carries the stamp. The pre-flight Step 0 in
   `quick-deploy.sh` resolves the host-side values automatically.
4. **Moving/rebasing after tagging.** Create the tag only when the current
   branch history is final. Rewriting the tagged commit confuses anyone who
   already fetched it.
5. **Using `--patch`.** `C` is now the build number, so `--patch` is rejected.
   Use `--release` / `--minor` for the next `B` release train, or `--major`
   when deliberately moving to the next `A` generation.
6. **Forgetting `pyproject.toml`.** The script handles this for you; just
   don't edit `pyproject.toml` `version` manually — it must always equal
   `web/package.json` `version`.
