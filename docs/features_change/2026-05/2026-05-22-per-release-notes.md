# Per-release feature change notes

## Motivation

The site's Change Log only groups feature-change notes by month, and the SemVer
tags produced by `scripts/dev/bump-version.sh` had no published artifact
beyond the git tag itself. Researchers and maintainers had no way to ask
"what shipped in v0.2.0?".

## User-facing change

- New top-level **Releases** section in the docs site:
  - `Releases → Overview` (`docs/releases/index.md`) lists every published
    `vX.Y.Z` with a one-line summary.
  - `Releases → v0.1.0` (`docs/releases/v0.1.0.md`) consolidates all 352
    feature-change notes that landed before per-release notes existed.
- From the next bump onward, `scripts/dev/bump-version.sh` automatically
  generates `docs/releases/vX.Y.Z.md` listing every `docs/features_change/**`
  file added between the previous tag and `HEAD`, and inserts the new page
  into both `docs/releases/index.md` and `mkdocs.yml` nav.
- New workflow `.github/workflows/release.yml` listens for `v[0-9]+.[0-9]+.[0-9]+`
  tag pushes and creates (or updates) the matching GitHub Release using
  `docs/releases/<tag>.md` as the body, so the GitHub Releases page mirrors
  the docs site exactly.

## API / IaC diff summary

- **docs**: `docs/releases/index.md` (new), `docs/releases/v0.1.0.md` (new,
  generated).
- **mkdocs**: nav gains a `Releases:` section between Change Log and User Guide.
- **scripts**: `scripts/dev/bump-version.sh` — added steps 10 + 11 to write
  the release page and append it to the index / nav before the existing
  commit + tag.
- **workflow**: `.github/workflows/release.yml` (new) — `contents: write`
  permission to publish GitHub Releases on tag push.
- No backend, frontend, or Bicep changes.

## Validation

- `bash -n scripts/dev/bump-version.sh` — syntax check passed.
- `scripts/dev/bump-version.sh --dry-run --minor` printed
  `[bump] 0.1.0 -> 0.2.0` without writing files.
- `uv run mkdocs build` — see terminal evidence in the PR.
