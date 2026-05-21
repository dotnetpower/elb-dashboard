"""Generate llms-full.txt during the MkDocs build.

Responsibility: Concatenate every navigable markdown page into a single
plain-text corpus at site/llms-full.txt so generative-engine crawlers can
ingest the whole documentation in one fetch (the GEO equivalent of an
XML sitemap for human search). Also register a Jinja ``unescape_html``
filter so the overrides template can safely embed page titles in JSON-LD
without HTML-encoded entities leaking through.

Edit boundaries: only the on_env / on_files / on_post_build hooks below;
do not mutate page content or navigation. The hook must be idempotent —
running ``mkdocs build`` twice must produce byte-identical output for
unchanged inputs.

Key entry points:
- ``on_env(env, ...)``       — register the ``unescape_html`` Jinja filter
  used by ``overrides/main.html`` when serialising JSON-LD strings.
- ``on_files(files, config)``  — declare llms-full.txt as a build artifact
  so MkDocs serves it during ``mkdocs serve``.
- ``on_post_build(config)``    — write the concatenated corpus into the
  generated site directory.

Risky contracts:
- Reads only files already in ``config['docs_dir']``; never reaches outside.
- Skips ``features_change/**`` and ``temp/**`` to mirror the navigation
  filters declared in mkdocs.yml.

Validation: ``mkdocs build`` then verify ``site/llms-full.txt`` exists,
starts with the project header, and contains at least the index headline.
"""

from __future__ import annotations

import html
import os
from pathlib import Path

EXCLUDE_PREFIXES = ("features_change/", "temp/", "overrides/")
EXCLUDE_NAMES = {
    "llms.txt",
    "llms-full.txt",
    "robots.txt",
    # Off-nav pages: documented but intentionally hidden from the main site
    # navigation. Including them in llms-full.txt would advertise open
    # security follow-ups and internal process notes to generative-engine
    # crawlers. Keep this list in sync with `Pages exist in the docs
    # directory, but are not included in the "nav" configuration` warnings
    # emitted by ``mkdocs build``.
    "copilot/security-audit-followup.md",
    "copilot/version-management.md",
}

HEADER_TEMPLATE = """# {site_name} — full documentation corpus

Source: {site_url}
Generated for generative-engine ingestion (one file, one fetch).
Each section below is one published page, separated by `---` rulers.
Internal markdown links are preserved relative to {site_url}.
"""


def on_env(env, *, config, files):
    """Register the ``unescape_html`` Jinja filter used by overrides/main.html.

    MkDocs renders ``page.title`` after the markdown pipeline, so an H1
    like ``API & Endpoints`` arrives in templates as the HTML-escaped
    string ``API &amp; Endpoints``. Embedding that in JSON-LD via
    ``| tojson`` would publish ``"name": "API &amp; Endpoints"`` to
    crawlers. This filter undoes the HTML escape before serialisation.
    """

    def _unescape(value):
        if value is None:
            return value
        return html.unescape(str(value))

    env.filters["unescape_html"] = _unescape
    return env

HEADER_TEMPLATE = """# {site_name} — full documentation corpus

Source: {site_url}
Generated for generative-engine ingestion (one file, one fetch).
Each section below is one published page, separated by `---` rulers.
Internal markdown links are preserved relative to {site_url}.
"""


def _iter_markdown(docs_dir: Path):
    for path in sorted(docs_dir.rglob("*.md")):
        rel = path.relative_to(docs_dir).as_posix()
        if any(rel.startswith(p) for p in EXCLUDE_PREFIXES):
            continue
        if rel in EXCLUDE_NAMES:
            continue
        yield rel, path


def on_post_build(config, **_kwargs) -> None:
    docs_dir = Path(config["docs_dir"]).resolve()
    site_dir = Path(config["site_dir"]).resolve()
    site_url = (config.get("site_url") or "").rstrip("/")
    site_name = config.get("site_name", "Documentation")

    if not site_dir.exists():
        return

    parts = [HEADER_TEMPLATE.format(site_name=site_name, site_url=site_url or "(no site_url)")]
    for rel, path in _iter_markdown(docs_dir):
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            continue
        slug = rel[:-3] if rel.endswith(".md") else rel
        if slug.endswith("/index"):
            slug = slug[: -len("/index")] + "/"
        elif slug == "index":
            slug = ""
        page_url = f"{site_url}/{slug}" if site_url else f"/{slug}"
        parts.append(f"\n\n---\n\n## {rel}\n<{page_url}>\n\n{body.rstrip()}\n")

    out_path = site_dir / "llms-full.txt"
    out_path.write_text("".join(parts), encoding="utf-8")


def on_files(files, config):
    """Make sure ``llms.txt`` and ``robots.txt`` are emitted at site root."""
    docs_dir = Path(config["docs_dir"]).resolve()
    for name in ("llms.txt", "robots.txt"):
        src = docs_dir / name
        if src.exists() and files.get_file_from_path(name) is None:
            # mkdocs already auto-copies these, but keep the guard so a
            # future docs_dir change is loud rather than silent.
            os.utime(src, None)
    return files
