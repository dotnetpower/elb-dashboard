"""Generate the tag index for docs/tags.md during the MkDocs build.

The free Material for MkDocs `tags` plugin renders per-page tag chips, but
the cross-page tag index page (clickable tag -> list of pages) is an
Insiders-only feature. This hook fills that gap: it scans every navigable
markdown page in `docs/`, extracts `tags:` frontmatter, and rewrites the
`[TAGS]` macro inside `docs/tags.md` with a grouped list.

Responsibility: produce the tag-by-tag index for the tags page.
Edit boundaries: only the on_page_markdown hook below. Do not mutate any
page other than the one whose source path ends in `tags.md`.
Key entry points:
- `on_page_markdown(markdown, page, config, files)` — replaces the
  `[TAGS]` placeholder on tags.md with a grouped tag listing.
Risky contracts:
- Re-reads source files from `config['docs_dir']` to extract frontmatter;
  must skip files that are excluded from nav (features_change/, temp/).
- Idempotent: running `mkdocs build` twice must produce identical HTML
  for unchanged inputs.
Validation: `uv run mkdocs build --clean --strict` and confirm
`site/tags/index.html` contains an `## architecture` heading with at
least one bullet link.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

EXCLUDE_PREFIXES = ("features_change/", "temp/", "overrides/")
PLACEHOLDER = "[TAGS]"


def _parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    block = text[4:end]
    rest = text[end + 5 :]

    meta: dict[str, object] = {}
    current_key: str | None = None
    current_list: list[str] | None = None
    for raw in block.splitlines():
        if not raw.strip():
            continue
        if raw.startswith("  - ") and current_list is not None:
            current_list.append(raw[4:].strip())
            continue
        if ":" not in raw or raw.startswith(" "):
            continue
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if not value:
            current_list = []
            meta[key] = current_list
            current_key = key
        else:
            meta[key] = value
            current_key = None
            current_list = None
    return meta, rest


def _collect(docs_dir: Path) -> dict[str, list[tuple[str, str]]]:
    """Return {tag: [(title, src_uri)]} sorted alphabetically."""
    grouped: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for md_path in sorted(docs_dir.rglob("*.md")):
        rel = md_path.relative_to(docs_dir).as_posix()
        if any(rel.startswith(p) for p in EXCLUDE_PREFIXES):
            continue
        if rel == "tags.md":
            continue
        try:
            text = md_path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, _ = _parse_frontmatter(text)
        tags = meta.get("tags")
        if not isinstance(tags, list) or not tags:
            continue
        title = meta.get("title")
        if not isinstance(title, str) or not title.strip():
            title = rel
        for tag in tags:
            grouped[str(tag).strip()].append((title.strip(), rel))
    for tag in grouped:
        grouped[tag].sort(key=lambda item: item[0].lower())
    return dict(sorted(grouped.items()))


def _render(grouped: dict[str, list[tuple[str, str]]]) -> str:
    lines: list[str] = []
    for tag, pages in grouped.items():
        lines.append(f"## {tag}")
        lines.append("")
        for title, src in pages:
            # Material strips the trailing .md and adds a /; mkdocs will
            # rewrite the relative link from tags.md to each target.
            link_target = src
            lines.append(f"- [{title}]({link_target})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def on_page_markdown(markdown, page, config, files):  # noqa: D401
    """Replace `[TAGS]` on tags.md with the generated index."""
    src = getattr(page.file, "src_path", "") or ""
    if Path(src).as_posix() != "tags.md":
        return markdown
    if PLACEHOLDER not in markdown:
        return markdown
    docs_dir = Path(config["docs_dir"])
    grouped = _collect(docs_dir)
    rendered = _render(grouped) if grouped else "_(no tagged pages found)_\n"
    return markdown.replace(PLACEHOLDER, rendered)
