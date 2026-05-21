# Version Management

> Extracted from `.github/copilot-instructions.md` §13 on 2026-05-22 to keep
> the always-loaded charter lean. Read this when bumping the release version
> or touching the header version stamp pipeline.

The control plane carries a small SemVer + short-SHA stamp in the SPA header
(`v0.1.0 · 4060551`). The version is bumped by a single script that reads
Conventional Commits since the last tag — no manual edits to `package.json`
or `pyproject.toml`.

---

## 1. SemVer policy

| Segment | Trigger | Decided by |
|---------|---------|------------|
| **MAJOR** | breaking change you decide to ship | manual (`--major` flag) |
| **MINOR** | any commit since last tag matches `^feat(\(.+\))?:` | auto |
| **PATCH** | no `feat:` but at least one `^fix(\(.+\))?:` commit | auto |

If a `BREAKING CHANGE` footer or `feat!:` / `fix!:` marker is detected in
the range, the script refuses to auto-bump and exits with code 2 — pass
`--major` to acknowledge. This prevents accidental MAJOR bumps from being
hidden behind a routine commit message.

`chore:`, `docs:`, `test:`, `refactor:`, `style:`, `build:`, `ci:` commits
do **not** trigger a bump. If nothing in the range warrants a bump, the
script exits 0 with `nothing to bump`. Force with `--patch` / `--minor` /
`--major` only when you have a reason that doesn't map to a commit type.

`web/package.json` is the source of truth. `pyproject.toml` is kept in
sync by the same script so the backend image carries the same version
identifier the SPA displays.

---

## 2. Header stamp pipeline

```
┌─────────────────────────────────────────────────────────────┐
│  scripts/dev/quick-deploy.sh frontend                       │
│   ├─ APP_VERSION   = node -p require('web/package.json')... │
│   ├─ GIT_COMMIT    = git rev-parse --short HEAD             │
│   └─ BUILD_TIME    = date -u +%Y-%m-%dT%H:%M:%SZ            │
│         │                                                   │
│         ▼ --build-arg                                       │
│  az acr build (web/Dockerfile)                              │
│   ├─ ARG APP_VERSION / GIT_COMMIT / BUILD_TIME              │
│   └─ ENV APP_VERSION=... GIT_COMMIT=... BUILD_TIME=...      │
│         │                                                   │
│         ▼ npm run build                                     │
│  web/vite.config.ts  define:                                │
│   ├─ __APP_VERSION__   = process.env.APP_VERSION   ?? pkg   │
│   ├─ __APP_COMMIT__    = process.env.GIT_COMMIT    ?? git   │
│   └─ __APP_BUILD_TIME__= process.env.BUILD_TIME    ?? now() │
│         │                                                   │
│         ▼ baked into dist/assets/index-*.js                 │
│  web/src/components/Layout.tsx → `v${__APP_VERSION__} · …`  │
└─────────────────────────────────────────────────────────────┘
```

**Why host-side resolution is required.** ACR build context excludes
`.git` (and even with it, the build container's WORKDIR is `/web`, not
the repo root). The vite `define` falls back to `git rev-parse` for
local `npm run dev` and `npm run build`, but production builds must pass
the values through `--build-arg`. The Dockerfile already declares the
three `ARG`s so a missing build-arg shows up as empty string, not as a
build failure.

**Where the values surface in the UI.** The caption is rendered next to
"Control Plane" in [web/src/components/Layout.tsx](../../web/src/components/Layout.tsx)
using the three injected globals declared in
[web/src/vite-env.d.ts](../../web/src/vite-env.d.ts). The native
`title=` tooltip carries the full triple (`Version: vX.Y.Z`, `Commit: …`,
`Built: <ISO>`).

---

## 3. Bumping the version

```bash
# Dry-run first to see what would happen.
scripts/dev/bump-version.sh --dry-run

# Auto (feat: → minor, fix: → patch).
scripts/dev/bump-version.sh

# Manual override.
scripts/dev/bump-version.sh --major
scripts/dev/bump-version.sh --minor
scripts/dev/bump-version.sh --patch

# Push the release commit + tag.
git push origin "$(git rev-parse --abbrev-ref HEAD)" --follow-tags
```

The script:

1. Reads the current version from `web/package.json` (source of truth).
2. Scans commits in `<last-v-tag>..HEAD` (or full history if no tag exists).
3. Decides the bump kind from Conventional Commits, refusing on
   `BREAKING CHANGE` unless `--major` is passed.
4. Rewrites `web/package.json` (via `node`) and the first
   `version = "…"` line in [pyproject.toml](../../pyproject.toml).
5. Creates `chore(release): vX.Y.Z` commit + annotated `vX.Y.Z` tag.
6. Does **not** push — that stays an explicit step so the maintainer can
   review the diff first.

---

## 4. Release workflow

Routine — feature shipped via PR, ready to cut a release:

1. Merge the PR(s) to `main` using Conventional Commits subjects.
2. `scripts/dev/bump-version.sh --dry-run` → confirm the bump kind.
3. `scripts/dev/bump-version.sh` → creates commit + tag locally.
4. Inspect: `git show HEAD` (release commit), `git tag -v vX.Y.Z` (tag).
5. `git push origin main --follow-tags` → cloud picks up the new tag for
   GitHub releases automation (if/when set up).
6. Deploy the frontend with the new SHA visible in the header:
   ```bash
   bash scripts/dev/quick-deploy.sh frontend
   ```
7. Open the cloud URL, hover the header version caption, confirm the
   commit short SHA matches `git rev-parse --short HEAD`.

Hotfix — branch from a tag, ship a PATCH:

```bash
git checkout -b hotfix/v0.1.1 v0.1.0
# … fix: … commit …
scripts/dev/bump-version.sh --patch
git push origin hotfix/v0.1.1 --follow-tags
```

---

## 5. Validation checklist

Before pushing a release tag:

- [ ] `scripts/dev/bump-version.sh --dry-run` shows the expected bump kind.
- [ ] `web/package.json` and `pyproject.toml` versions match (`git diff HEAD~1`).
- [ ] `cd web && npm run build` succeeds and `grep -oE '"<short-sha>"' dist/assets/index-*.js` returns the new SHA.
- [ ] Header tooltip shows the expected `Version: vX.Y.Z` after page reload.
- [ ] No other unrelated diffs in the release commit (the script refuses if
      `package.json`/`pyproject.toml` already have unstaged edits).

---

## 6. Common mistakes

1. **Editing `package.json` version by hand.** The script will still run,
   but the commit history won't have the `chore(release): vX.Y.Z` marker
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
4. **Bumping inside a feature branch.** Keep the bump on the same branch
   that will land on `main` (or on the hotfix branch). Bumping then
   rebasing rewrites the tag's target commit and confuses anyone who
   already pulled it.
5. **Forgetting `pyproject.toml`.** The script handles this for you; just
   don't edit `pyproject.toml` `version` manually — it must always equal
   `web/package.json` `version`.
