# Commit-channel upgrade execution (install latest main commit)

## Motivation
The previous change made the commit channel **discovery-only**: the toggle ON
surfaced a "new commit" badge, but the upgrade page could only install tagged
releases. Following up on that critique gap, the toggle now controls the actual
**install target**:

- **Toggle ON** → the latest tracking-branch (`main`) commit is an installable
  upgrade target (cloned, built, and deployed exactly like a release).
- **Toggle OFF** → only release tags (`vX.Y.Z`) are installable (unchanged).

## Key design — one load-bearing string
The whole pipeline pivots on `target_version`, which is simultaneously (1) the
image tag `v<ver>`, (2) the `APP_VERSION` build-arg baked into `api.__version__`
and the SPA stamp, and (3) the reconciler's **string-equality** success gate
`api.__version__ == target_version`. Because the gate is string equality (no
semver parse), a commit reuses the entire existing state machine by baking a
commit version string. The only place that branches on release-vs-commit is the
git clone.

Commit version string: `<base_release>-commit.<short_sha>` (e.g.
`0.2.0-commit.a1b2c3d`) — Docker-tag safe as `v0.2.0-commit.a1b2c3d`, derived
server-side from the running release base + the discovered commit sha.

## API / IaC diff summary
- **New** `api/services/upgrade/version_target.py` — release/commit string
  contract: `is_release_version`, `is_commit_version`, `is_valid_target_version`,
  `base_release`, `make_commit_version`, `commit_short_sha`.
- `git_workspace.clone(..., target_kind, target_sha)` — release =
  `git clone --depth 1 --branch v<ver>` (unchanged); commit =
  `git clone --filter=blob:none --no-checkout` + `git checkout --detach <40-hex>`.
  Validation regex widened to accept the commit form; commit clone requires a
  full 40-hex sha.
- `image_builder` — validation widened to the commit form; the frontend commit
  build additionally passes `--build-arg GIT_COMMIT=<short_sha>` so the SPA
  header + `isCommitUpdateAvailable` clear after the upgrade lands.
- `state.UpgradeState.target_kind: str = "release"` — round-tripped through the
  Tables entity converters; legacy rows default to "release".
- `start` route — accepts `target_kind` ("release"|"commit"); commit path
  requires the `track_commits` toggle ON (409 otherwise) + a full 40-hex
  `target_sha`, and derives `target_version` server-side via
  `make_commit_version(base_release(api.__version__), sha)`. Release path
  unchanged (still requires a semver `target_version`).
- `start_upgrade_inline(..., target_kind)` persists the kind on the row;
  `execute_upgrade_inline` reads `target_kind`/`target_sha` from the row for the
  clone (Celery task signature unchanged → in-flight tasks during a deploy keep
  deserialising).
- `remote_tags.filter_candidates` reduces a commit running-version to its base
  release before the `packaging.Version` compare.
- **Reconciler: no change** — `__version__ == target_version` and
  `_image_matches_version` (tag `v<ver>`) are already string-generic.
- SPA: `upgrade.ts` `UpgradeStartRequest` gains `target_kind` + optional
  `target_version`; `UpgradePage` adds a "main @ <sha> (latest commit)" option
  in the target `<select>` (shown only when `track_commits` and
  `isCommitUpdateAvailable`), encoded as `commit:<full_sha>`, and sends
  `target_kind="commit"` + `target_sha` on start. This resolves the prior
  "badge with no installable target" gap.

## Security / safety
- The commit sha flows only into an allow-listed `git` argv (validated 40-hex);
  the remote URL still comes solely from env/default (no SSRF surface change).
- Commit start is gated by `require_upgrade_admin` + `confirm_downtime` (same as
  release) PLUS the server-side `track_commits` check (defense in depth — the
  SPA hides the option when off, but a direct API call is rejected too).
- Unreachable sha → `git checkout` fails → `_fail_pre`; the reconciler's
  per-state stuck guards bound the worst case.

## Validation
- `uv run pytest -q api/tests` — 2895 passed, 3 skipped.
  - New: `test_upgrade_version_target.py` (10), commit clone tests in
    `test_upgrade_git_workspace.py`, commit tag/GIT_COMMIT in
    `test_upgrade_image_builder.py`, commit-start matrix in
    `test_upgrade_routes.py`, full commit execution in `test_upgrade_task.py`.
- `uv run ruff check` (touched) — clean. Facade-contract + chaos + bluegreen — green.
- `cd web && npm run build` — OK; `npx vitest run` — 670 passed; eslint — clean.
