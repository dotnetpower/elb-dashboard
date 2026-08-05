---
title: Bound the GitHub Release body so a large release can publish
description: The Releases API rejects a body over 125,000 characters, so the v0.3.0 publish failed with HTTP 422 after the renderer produced 753 notes. Build the body through a tested helper that truncates on a line boundary and links the full rendered page.
tags: [release, contributor]
---

# Bound the GitHub Release body so a large release can publish

## Motivation

Tagging `v0.3.0` produced a correct release-notes page — `v0.2.0` was tagged on
2026-05-22, so the range legitimately covers **724 commits / 753 feature-change
notes** — but the "Publish Release Notes" workflow then failed:

```
HTTP 422: Validation Failed (https://api.github.com/repos/dotnetpower/elb-dashboard/releases)
body is too long (maximum is 125000 characters)
```

The workflow pasted the entire rendered page into the release body with `awk`.
`docs/releases/v0.3.0.md` is ~195 KB, so the API refused it and **no GitHub
Release was created at all**. The renderer was not at fault; the publish step
had no size contract. Any future release that accumulates enough notes hits the
same wall, and the failure mode is "no release", not "a shorter release".

## User-facing change

`git push --follow-tags` now publishes a release regardless of how many notes
the range contains. When the notes exceed the API limit, the body carries as
many complete entries as fit and ends with an explicit pointer:

> _Release notes truncated to fit GitHub's 125,000-character limit. The complete
> list is on the [docs site](https://dotnetpower.github.io/elb-dashboard/releases/)._

The docs site remains the canonical, complete rendered view — it was already
linked from the body header and is unaffected by this change.

## Change summary

* New [scripts/dev/build_release_body.py](../../../scripts/dev/build_release_body.py):
  drops the duplicated H1 (the Releases UI already shows the tag as the title),
  prepends the provenance header, and truncates **on a line boundary** so the
  markdown never ends inside a half-written link. Default limit 120,000
  characters, deliberately below GitHub's 125,000 hard cap so a later
  header/footer tweak cannot silently push a release over the edge. The
  truncation footer is reserved out of the budget up front, so it can never be
  the thing that gets cut — losing it would strip the body's only link to the
  complete notes.
* [.github/workflows/release.yml](../../../.github/workflows/release.yml): the
  "Build release body" step calls the helper instead of inlining `awk`.
* No change to `scripts/dev/render_release_notes.py` — the rendered page is
  correct and stays complete.

## Validation

* `uv run pytest -q api/tests/test_build_release_body.py` — 4 passed:
  * `test_small_release_is_passed_through_untruncated`
  * `test_oversized_release_is_bounded_and_keeps_the_pointer`
  * `test_default_limit_stays_below_the_github_hard_cap`
  * `test_real_release_page_fits_when_present` — drives the actual checked-in
    `docs/releases/v0.3.0.md`, the page that caused the failure.
* Dry run against the real page:
  `python3 scripts/dev/build_release_body.py --notes docs/releases/v0.3.0.md --out /tmp/release-body.md`
  → `release body: 119957 chars (limit 120000, truncated=true)`, ending with the
  truncation footer and the docs link.
* `uv run ruff check api` — clean. `uv run pytest -q api/tests` — full suite green.

## Operational note

`v0.3.0` was tagged before this fix existed, and the publish workflow checks out
the tag it publishes — so re-running it for `v0.3.0` would not find the new
helper. Rather than force-move a pushed tag, `v0.3.0` was published once by
running the *same* helper locally and handing its output to
`gh release create`, which is byte-for-byte what the fixed workflow does:

```bash
python3 scripts/dev/build_release_body.py \
  --notes docs/releases/v0.3.0.md --out /tmp/release-body.md
gh release create v0.3.0 --notes-file /tmp/release-body.md --title v0.3.0
```

This keeps the `v0.3.0` tag pointing at its `chore(release): v0.3.0` commit,
which is where it belongs. Every subsequent tag picks the fix up automatically
from the workflow.
