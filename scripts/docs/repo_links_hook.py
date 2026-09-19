"""Rewrite repository source links for the published documentation site.

Responsibility: Convert Markdown-generated links that resolve outside
    ``docs/`` but inside this repository into GitHub ``blob`` or ``tree`` URLs.
Edit boundaries: Mutate only rendered anchor ``href`` attributes. Internal
    documentation links, external URLs, fragments, and missing paths stay
    unchanged.
Key entry points: ``rewrite_repo_links``, ``on_page_content``.
Risky contracts: Preserve line fragments and never rewrite a path outside the
    repository; source links must work both in local MkDocs and GitHub Pages.
Validation: ``uv run pytest -q api/tests/test_repo_links_hook.py`` and
    ``DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict``.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from urllib.parse import quote, urlsplit

REPOSITORY_URL = "https://github.com/dotnetpower/elb-dashboard"
_HREF_RE = re.compile(r'(?P<prefix>\bhref=")(?P<href>[^"]+)(?P<suffix>")')


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _source_url(href: str, *, source_path: Path, docs_dir: Path) -> str | None:
    decoded = html.unescape(href)
    parsed = urlsplit(decoded)
    if parsed.scheme or parsed.netloc or not parsed.path or parsed.path.startswith("/"):
        return None

    docs_root = docs_dir.resolve()
    repo_root = docs_root.parent
    target = (source_path.resolve().parent / parsed.path).resolve()
    if _is_within(target, docs_root) or not _is_within(target, repo_root):
        return None
    if not target.exists():
        return None

    relative = target.relative_to(repo_root).as_posix()
    kind = "tree" if target.is_dir() else "blob"
    rewritten = f"{REPOSITORY_URL}/{kind}/main/{quote(relative, safe='/')}"
    if parsed.query:
        rewritten += f"?{parsed.query}"
    if parsed.fragment:
        rewritten += f"#{parsed.fragment}"
    return rewritten


def rewrite_repo_links(content: str, *, source_path: Path, docs_dir: Path) -> str:
    """Return rendered page content with repository source links rewritten."""

    def replace(match: re.Match[str]) -> str:
        href = match.group("href")
        rewritten = _source_url(href, source_path=source_path, docs_dir=docs_dir)
        if rewritten is None:
            return match.group(0)
        return f"{match.group('prefix')}{rewritten}{match.group('suffix')}"

    return _HREF_RE.sub(replace, content)


def on_page_content(content: str, page, config, files) -> str:
    """MkDocs hook: make repository source links valid on GitHub Pages."""
    del files
    source_path = Path(page.file.abs_src_path)
    docs_dir = Path(config["docs_dir"])
    return rewrite_repo_links(content, source_path=source_path, docs_dir=docs_dir)
