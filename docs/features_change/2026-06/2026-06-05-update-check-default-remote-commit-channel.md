# Update check: zero-config default remote + "new commits" channel toggle

## Motivation
Two papercuts in Settings → Updates:

1. **Required manual configuration.** The upgrade subsystem stayed inert until
   an operator set `UPGRADE_GIT_REMOTE` on the Container App, so a fresh
   control plane showed "NOT CONFIGURED" and never surfaced updates.
2. **Releases-only discovery.** The check only looked at `refs/tags/vX.Y.Z`,
   so a control plane sitting between releases could not see that the tracking
   branch had moved ahead — there was no "preview / new commits" channel like
   most apps offer.

## User-facing change
- **Works with zero configuration.** When `UPGRADE_GIT_REMOTE` is unset, the
  check now falls back to the project's own public remote
  (`DEFAULT_GIT_REMOTE = https://github.com/dotnetpower/elb-dashboard.git`).
  Operators can still override via the env var. The "Not configured" state is
  only reachable by blanking the in-code default.
- **New "Allow updates from new commits" toggle (default ON).** Settings →
  Updates gains a switch:
  - **ON (default)** — discovery also surfaces the latest commit on the
    tracking branch (`main`). The "Latest available" badge reads
    `new commit <short-sha>` when the branch has moved past the running build
    and no newer release tag exists.
  - **OFF** — only tagged releases are checked (the previous behaviour); the
    commit indicator is cleared.
- The toggle is persisted **server-side** (upgrade-state row) so the periodic
  beat discovery honours it across revisions. Flipping it re-runs a discovery
  check immediately so the badge updates without waiting for the next poll.
- The gear-dot / "Latest available" badge now lights up for a new commit (when
  the channel is on) in addition to a newer release.

## API / IaC diff summary
- `api/services/upgrade/remote_tags.py`
  - `DEFAULT_GIT_REMOTE`, `DEFAULT_TRACK_BRANCH` constants.
  - `configured_remote()` falls back to the in-code default (env still wins).
  - Extracted `_advertise_refs` / `_tags_from_refs` / `_branch_head_from_refs`;
    added `fetch_branch_head(remote, *, branch)` returning the tracking-branch
    HEAD sha (or `""`). `fetch_release_tags` stays the primary, well-tested
    seam.
- `api/services/upgrade/state.py` — new `track_commits: bool = True` and
  `latest_commit_sha: str = ""` fields, round-tripped through the Tables entity
  converters; `_coerce_bool` defaults a legacy row's missing column to ON.
- `api/tasks/upgrade/pipeline.py` — `check_latest_inline` is channel-aware:
  release tags first (always), then a **best-effort** branch-head fetch when
  `track_commits` is on. A branch-head failure never sinks the release check.
- `api/routes/upgrade.py`
  - `POST /api/upgrade/settings { track_commits: bool }` (`require_caller`,
    consistent with the existing any-caller `POST /check`) persists the toggle.
  - `_mask_state` fills the response `git_remote` with the effective (masked)
    remote when the persisted row has none yet, so a cold-start control plane
    never shows "not configured".
- SPA: `web/src/api/upgrade.ts` (`track_commits` + `latest_commit_sha` on
  `UpgradeStatus`, `setTrackCommits`, `isCommitUpdateAvailable`),
  `useUpgradeAvailability` (commit-aware `available`, `applyStatus`),
  `SettingsPanel` Updates section (channel toggle + commit badge + copy).
- No Bicep / RBAC / network changes. The default remote is a hardcoded,
  trusted, anonymous HTTPS URL — never caller-controlled — so the SSRF posture
  of `remote_tags` is unchanged.

## Scope boundary (follow-up)
This change makes the **check/discovery** channel-aware and config-free. The
upgrade **execution** pipeline (`git_workspace.clone`, `image_builder`,
`reconciler`, blue/green, rollback) remains keyed on `vA.B.C` release-tag
semver and `api.__version__`. Building/deploying an arbitrary tracking-branch
commit is a larger, separate change (commit→semver resolution at clone time,
backend version stamping, reconciler version-match) and is intentionally out of
scope here.

## Validation
- `uv run pytest -q api/tests` — 2872 passed, 3 skipped.
  - New: `test_configured_remote_*`, `test_fetch_branch_head_*`,
    `test_status_fills_effective_default_remote`,
    `test_settings_toggles_track_commits`,
    `test_check_commit_channel_*`, `test_check_release_only_skips_branch_head`,
    `test_track_commits_*` (state round-trip + legacy default).
- `uv run ruff check api/...` (touched files) — clean.
- `cd web && npm run build` — OK; `npm test -- --run` — 637 passed.
- `npx eslint` on touched SPA files — clean.
