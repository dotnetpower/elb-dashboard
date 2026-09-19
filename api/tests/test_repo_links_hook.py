"""Tests for the published repository-source link rewriter.

Responsibility: Pin conversion of out-of-docs repository links to GitHub while
    preserving internal, external, fragment-only, and missing-path links.
Edit boundaries: Pure temporary-filesystem tests; no MkDocs build or network.
Key entry points: ``test_rewrite_repo_links``.
Risky contracts: Line fragments and directory/file URL kinds must survive, and
    paths escaping the repository must never be rewritten.
Validation: ``uv run pytest -q api/tests/test_repo_links_hook.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "docs" / "repo_links_hook.py"


@pytest.fixture(scope="module")
def hook() -> Any:
    spec = importlib.util.spec_from_file_location("repo_links_hook", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rewrite_repo_links(hook: Any, tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    source = docs_dir / "copilot" / "map.md"
    internal = docs_dir / "architecture" / "index.md"
    code_file = tmp_path / "api" / "main.py"
    code_dir = tmp_path / "web" / "src"
    for path in (source, internal, code_file):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    code_dir.mkdir(parents=True)

    content = """\
<a href="../../api/main.py#L10-L12">file</a>
<a href="../../web/src/">dir</a>
<a href="../architecture/index.md">docs</a>
<a href="https://example.com/x">external</a>
<a href="#local">fragment</a>
<a href="../../api/missing.py">missing</a>
"""

    rewritten = hook.rewrite_repo_links(content, source_path=source, docs_dir=docs_dir)

    assert (
        'href="https://github.com/dotnetpower/elb-dashboard/blob/main/api/main.py#L10-L12"'
        in rewritten
    )
    assert 'href="https://github.com/dotnetpower/elb-dashboard/tree/main/web/src"' in rewritten
    for unchanged in (
        'href="../architecture/index.md"',
        'href="https://example.com/x"',
        'href="#local"',
        'href="../../api/missing.py"',
    ):
        assert unchanged in rewritten
