"""Tests for the GitHub Release body builder.

Responsibility: Pin the size contract that broke the v0.3.0 publish — the
    GitHub Releases API rejects a body over 125,000 characters with HTTP 422,
    and `docs/releases/v0.3.0.md` rendered ~195 KB. Verify the builder stays
    under the limit, truncates on a line boundary, always keeps the pointer to
    the full page, and leaves a small release untouched.
Edit boundaries: Pure text-shaping assertions. The script lives outside the
    `api/` import tree, so it is loaded via
    `importlib.util.spec_from_file_location` rather than adding `scripts/` to
    `sys.path` for every test session (same pattern as
    `test_check_rbac_removal.py`).
Key entry points: the `test_*` functions.
Risky contracts: a truncated body must never exceed the limit and must never
    drop the truncation footer, or the release silently loses its only link to
    the complete notes.
Validation: ``uv run pytest -q api/tests/test_build_release_body.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "dev" / "build_release_body.py"
)


@pytest.fixture(scope="module")
def builder() -> Any:
    spec = importlib.util.spec_from_file_location("build_release_body", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_small_release_is_passed_through_untruncated(builder: Any) -> None:
    notes = "# v0.9.0\n\n- `2026-08-05` — [Something](../features_change/x.md)\n"

    body, truncated = builder.build_body(notes, notes_path="docs/releases/v0.9.0.md")

    assert truncated is False
    # The H1 is dropped (the Releases UI already renders the tag as the title).
    assert "# v0.9.0" not in body
    assert "docs/releases/v0.9.0.md" in body
    assert "[Something](../features_change/x.md)" in body
    assert "truncated" not in body


def test_oversized_release_is_bounded_and_keeps_the_pointer(builder: Any) -> None:
    # Mirrors the real failure: v0.3.0 rendered 753 notes / ~195 KB and the API
    # rejected it with "body is too long (maximum is 125000 characters)".
    entry = "- `2026-08-05` — [Note](../features_change/2026-08/note.md)\n"
    notes = "# v0.3.0\n\n" + entry * 4000
    limit = 10_000

    body, truncated = builder.build_body(
        notes, notes_path="docs/releases/v0.3.0.md", limit=limit
    )

    assert truncated is True
    assert len(body) <= limit
    assert "Release notes truncated" in body
    assert builder.DEFAULT_DOCS_URL in body
    # Truncation happens on a line boundary, so the markdown never ends inside a
    # half-written link.
    assert "](../features_change/2026-08/note.md)\n" in body


def test_default_limit_stays_below_the_github_hard_cap(builder: Any) -> None:
    """125,000 is GitHub's documented cap; the default must leave headroom."""
    assert builder.DEFAULT_LIMIT < 125_000


def test_real_release_page_fits_when_present(builder: Any) -> None:
    """The checked-in v0.3.0 page is the regression case — it must now fit."""
    page = Path(__file__).resolve().parents[2] / "docs" / "releases" / "v0.3.0.md"
    if not page.exists():  # pragma: no cover - page removed in a future cleanup
        pytest.skip("docs/releases/v0.3.0.md not present")

    body, truncated = builder.build_body(
        page.read_text(encoding="utf-8"), notes_path=str(page)
    )

    assert truncated is True  # ~195 KB of notes
    assert len(body) < 125_000
    assert "Release notes truncated" in body
