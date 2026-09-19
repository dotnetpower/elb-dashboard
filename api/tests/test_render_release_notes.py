"""Regression tests for release-note target-tree filtering.

Responsibility: Verify the docs release renderer excludes notes deleted before
    the target ref, preserves surviving metadata, and ignores headings inside
    fenced examples.
Edit boundaries: Pure renderer collection behavior; no release files are written.
Key entry points: the ``test_*`` functions.
Risky contracts: A deleted or pre-rename path must never become a broken link in
    a tagged release page; fenced comments must not become published titles.
Validation: ``uv run pytest -q api/tests/test_render_release_notes.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "render_release_notes.py"


@pytest.fixture(scope="module")
def renderer() -> Any:
    spec = importlib.util.spec_from_file_location("render_release_notes", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_collect_entries_excludes_paths_missing_at_target(
    renderer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept = "docs/features_change/2026-09/2026-09-16-kept.md"
    deleted = "docs/features_change/2026-09/2026-09-16-deleted.md"
    monkeypatch.setattr(renderer, "added_files", lambda _sha: [kept, deleted])
    monkeypatch.setattr(
        renderer,
        "path_exists_at_ref",
        lambda rel, target: target == "v1.2.0" and rel == kept,
    )
    monkeypatch.setattr(
        renderer,
        "read_title",
        lambda rel, _target_ref: ("2026-09-16", Path(rel).stem),
    )

    entries, feat_fix_count = renderer.collect_entries([("a" * 40, "feat: refresh docs")], "v1.2.0")

    assert feat_fix_count == 1
    assert [entry["rel"] for entry in entries] == ["../features_change/2026-09/2026-09-16-kept.md"]


def test_release_range_respects_target_without_a_start_tag(renderer: Any) -> None:
    assert renderer.release_range("", "v0.1.0") == (
        "v0.1.0",
        "(repository root)..v0.1.0",
    )
    assert renderer.release_range("v0.2.0", "v0.3.0") == (
        "v0.2.0..v0.3.0",
        "v0.2.0..v0.3.0",
    )


def test_read_title_prefers_frontmatter_over_fenced_comments(
    renderer: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rel = "docs/features_change/2026-06/2026-06-17-example.md"
    note = tmp_path / rel
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\n"
        "title: AKS Auto-Stop Cumulative Extension Fix\n"
        "description: Regression fixture.\n"
        "---\n\n"
        "## Motivation\n\n"
        "```python\n"
        "# OLD (before)\n"
        "```\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(renderer, "REPO_ROOT", tmp_path)

    assert renderer.read_title(rel) == (
        "2026-06-17",
        "AKS Auto-Stop Cumulative Extension Fix",
    )


def test_read_title_ignores_fenced_h1_without_frontmatter(
    renderer: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rel = "docs/features_change/2026-06/2026-06-18-example.md"
    note = tmp_path / rel
    note.parent.mkdir(parents=True)
    note.write_text(
        "```shell\n# NOT THE TITLE\n```\n\n# Actual Title\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(renderer, "REPO_ROOT", tmp_path)

    assert renderer.read_title(rel) == ("2026-06-18", "Actual Title")


def test_read_title_uses_content_from_target_ref(
    renderer: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rel = "docs/features_change/2026-06/2026-06-19-example.md"
    note = tmp_path / rel
    note.parent.mkdir(parents=True)
    note.write_text("# Current Working Tree Title\n", encoding="utf-8")
    monkeypatch.setattr(renderer, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        renderer.subprocess,
        "check_output",
        lambda *args, **kwargs: "---\ntitle: Historical Tag Title\n---\n",
    )

    assert renderer.read_title(rel, "v1.2.0") == (
        "2026-06-19",
        "Historical Tag Title",
    )
