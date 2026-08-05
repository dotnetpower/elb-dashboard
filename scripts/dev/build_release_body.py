"""Build a GitHub Release body from a rendered release-notes page.

The GitHub Releases API rejects a body longer than 125,000 characters with
``HTTP 422 Validation Failed: body is too long``. A release that spans many
feature-change notes overruns that easily — ``docs/releases/v0.3.0.md`` rendered
753 notes / ~195 KB and failed the publish step outright. The docs site is the
canonical rendered view, so the release body is bounded here and points at it
instead of failing the release.

Usage:
  build_release_body.py --notes docs/releases/v0.3.0.md --out release-body.md \\
      [--docs-url https://…/releases/] [--limit 120000]

Responsibility: Turn one rendered release-notes markdown page into a
    GitHub-Release-safe body: drop the duplicated H1, prepend a provenance
    header, and truncate on a line boundary with an explicit pointer when the
    page exceeds the API limit.
Edit boundaries: Pure text shaping plus argv/file IO. No git, no network, no
    GitHub API calls — the workflow owns publishing.
Key entry points: ``build_body``, ``main``.
Risky contracts: The returned body MUST stay at or under ``limit`` characters
    (the caller passes a value below GitHub's 125,000 hard cap). Truncation MUST
    happen on a line boundary so the markdown never ends mid-link, and the
    truncation footer MUST always survive — it carries the link to the full page.
Validation: ``uv run pytest -q api/tests/test_build_release_body.py``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

# GitHub's documented hard cap is 125,000 characters. Default a little below it
# so a future header/footer tweak cannot silently push a release over the edge.
DEFAULT_LIMIT = 120_000
DEFAULT_DOCS_URL = "https://dotnetpower.github.io/elb-dashboard/releases/"


def build_body(
    notes_text: str,
    *,
    notes_path: str,
    docs_url: str = DEFAULT_DOCS_URL,
    limit: int = DEFAULT_LIMIT,
) -> tuple[str, bool]:
    """Return ``(body, truncated)`` for one rendered release-notes page.

    The leading H1 is dropped because the GitHub Releases UI already shows the
    tag name as the title. When the page does not fit, whole lines are kept
    until the budget is spent and a footer linking to the full page is appended,
    so the body is always valid markdown and never silently loses the pointer.
    """
    lines = notes_text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]

    header = (
        f"Generated from `{notes_path}`. "
        f"See the [docs site]({docs_url}) for the rendered view.\n\n"
    )
    footer = (
        "\n---\n\n_Release notes truncated to fit GitHub's 125,000-character "
        f"limit. The complete list is on the [docs site]({docs_url})._\n"
    )

    budget = limit - len(header) - len(footer)
    if budget <= 0:  # pragma: no cover - only a nonsensical --limit gets here
        raise ValueError("limit is too small to hold the header and footer")

    kept: list[str] = []
    used = 0
    truncated = False
    for line in lines:
        cost = len(line) + 1  # the newline this line contributes
        if used + cost > budget:
            truncated = True
            break
        kept.append(line)
        used += cost

    body = header + "\n".join(kept)
    body = body + footer if truncated else body + "\n"
    return body, truncated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notes", required=True, help="rendered release-notes page")
    parser.add_argument("--out", required=True, help="where to write the body")
    parser.add_argument("--docs-url", default=DEFAULT_DOCS_URL)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()

    notes = Path(args.notes)
    body, truncated = build_body(
        notes.read_text(encoding="utf-8"),
        notes_path=str(notes),
        docs_url=args.docs_url,
        limit=args.limit,
    )
    Path(args.out).write_text(body, encoding="utf-8")
    print(
        f"release body: {len(body)} chars "
        f"(limit {args.limit}, truncated={str(truncated).lower()})"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
