"""Frontmatter guard for ElasticBLAST docs.

Responsibility: fail CI when a navigated docs page is missing `title:`,
`description:`, or carries a tag that is not in the documented canon.
Edit boundaries: this script only reads `docs/**/*.md`; it does not
mutate files. It exits with code 1 on the first finding so the failure
is easy to read in CI logs.
Key entry points:
- `main()` returns 0 on success, 1 on any finding.
- Excluded from checks: `features_change/**`, `temp/**`, `overrides/**`.
Risky contracts:
- The canonical tag list duplicates `docs/tags.md` Canon table. If you
  introduce a new tag, add it to both files in the same change.
Validation: `uv run python scripts/docs/check_frontmatter.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "docs"

EXCLUDE_PREFIXES = ("features_change/", "temp/", "overrides/")
EXCLUDE_NAMES = {"llms.txt", "llms-full.txt", "robots.txt"}

CANON_TAGS = {
    "overview",
    "setup",
    "user-guide",
    "operate",
    "architecture",
    "infra",
    "auth",
    "security",
    "blast",
    "terminal",
    "ui",
    "agent",
    "research",
    "contributor",
    "release",
}

REQUIRED_KEYS = ("title", "description")


def _parse_frontmatter(text: str) -> tuple[dict[str, object], bool]:
    if not text.startswith("---\n"):
        return {}, False
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, False
    block = text[4:end]

    meta: dict[str, object] = {}
    current_list: list[str] | None = None
    for raw in block.splitlines():
        if not raw.strip():
            continue
        if raw.startswith("  - ") and current_list is not None:
            current_list.append(raw[4:].strip())
            continue
        if raw.startswith("  "):
            continue
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if not value:
            current_list = []
            meta[key] = current_list
        else:
            meta[key] = value
            current_list = None
    return meta, True


def _iter_pages():
    for path in sorted(ROOT.rglob("*.md")):
        rel = path.relative_to(ROOT).as_posix()
        if any(rel.startswith(p) for p in EXCLUDE_PREFIXES):
            continue
        if rel in EXCLUDE_NAMES:
            continue
        yield path, rel


def main() -> int:
    findings: list[str] = []
    for path, rel in _iter_pages():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            findings.append(f"{rel}: read error: {exc}")
            continue
        meta, has_fm = _parse_frontmatter(text)
        if not has_fm:
            findings.append(f"{rel}: missing frontmatter block")
            continue
        for key in REQUIRED_KEYS:
            value = meta.get(key)
            if not isinstance(value, str) or not value.strip():
                findings.append(f"{rel}: missing or empty `{key}:` in frontmatter")
        tags = meta.get("tags")
        if isinstance(tags, list):
            for tag in tags:
                if tag not in CANON_TAGS:
                    findings.append(
                        f"{rel}: tag `{tag}` not in CANON_TAGS "
                        f"(see scripts/docs/check_frontmatter.py and docs/tags.md)"
                    )
    if findings:
        print("FAIL — frontmatter guard found {0} issue(s):".format(len(findings)))
        for line in findings:
            print(f"  {line}")
        return 1
    print(f"OK — frontmatter guard checked {sum(1 for _ in _iter_pages())} navigated pages.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
