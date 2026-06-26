"""Generate a grouped changelog index for `docs/changelog.md`.

Responsibility: scan `docs/features_change/YYYY-MM/YYYY-MM-DD-<slug>.md` and
emit a month-by-month, category-grouped index. Replaces the `[CHANGELOG]`
placeholder on `docs/changelog.md` with the generated content.

Edit boundaries: only the on_page_markdown hook below. The hook never
mutates change notes themselves. Idempotent.

Key entry points:
- `on_page_markdown(markdown, page, config, files)` — replaces the
  `[CHANGELOG]` placeholder on changelog.md with grouped index.

Risky contracts:
- Category mapping (CATEGORY_PREFIXES) is the source of truth for how
  per-feature notes are bucketed. Adding a new prefix here is the only
  place to teach the changelog about a new product area.
- The first H1 of each note becomes its title. If a note lacks `# `, the
  filename slug (without date) is used.

Validation: `uv run mkdocs build --clean --strict` then confirm
`site/changelog/index.html` contains both a `## 2026-05` heading and at
least one nested `### BLAST` (or similar) sub-heading.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

PLACEHOLDER = "[CHANGELOG]"

# Order matters: more specific prefixes first.
CATEGORY_PREFIXES: list[tuple[str, tuple[str, ...]]] = [
    (
        "Security",
        (
            "security-",
            "auth-",
            "msal-",
            "rbac-",
            "production-hardening",
            "production-feature-flags",
        ),
    ),
    (
        "BLAST",
        (
            "blast-",
            "web-blast-",
            "ncbi-",
            "taxonomy-",
            "primer-",
            "core-nt-",
            "e16-",
            "outfmt5-",
            "alignments-",
        ),
    ),
    ("AKS / Cluster", ("aks-", "cluster-", "k8s-", "cluster-")),
    ("Terminal", ("terminal-",)),
    ("Storage / DB", ("storage-", "db-", "warmup-", "auto-warmup-", "prepare-db-")),
    (
        "OpenAPI",
        (
            "openapi-",
            "elb-openapi-",
            "external-elastic-blast",
            "api-endpoints-",
            "api-reference-",
            "api-submit-",
            "api-response-",
            "api-core-",
        ),
    ),
    (
        "Container Apps / Infra",
        (
            "container-app-",
            "container-apps-",
            "postprovision-",
            "infra-",
            "frontend-sidecar-",
            "sidecar-",
            "sidecars-",
            "lean-azd-",
            "cloud-init-",
            "azd-up-",
        ),
    ),
    (
        "Dashboard / UI",
        (
            "dashboard-",
            "ui-",
            "jobs-",
            "results-",
            "new-search-",
            "submit-",
            "wizard-",
            "resource-",
            "frontend-",
            "light-theme-",
            "ms-brand-",
            "mono-",
            "mock-",
            "ncbi-blast-ux-",
            "premium-",
            "completed-progress-",
            "command-preview-",
            "execution-steps-",
            "first-run-",
            "recent-search-",
            "step-log-",
        ),
    ),
    ("Self-upgrade", ("self-upgrade-",)),
    (
        "Local dev",
        ("local-", "fast-debug-", "dev-compose-", "vscode-", "scripts-"),
    ),
    (
        "Docs",
        (
            "docs-",
            "mkdocs-",
            "get-started-",
            "joining-",
            "user-guide-",
            "changelog-",
            "per-release-",
            "release-",
        ),
    ),
    (
        "Deploy",
        (
            "deploy-",
            "azure-deployment",
            "managed-identity-",
            "workload-",
            "release-build-",
            "release-notes-",
        ),
    ),
]


def _read_title(path: Path, fallback: str) -> str:
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("# "):
                    return stripped[2:].strip()
    except OSError:
        pass
    return fallback


def _category_for(slug_without_date: str) -> str:
    for label, prefixes in CATEGORY_PREFIXES:
        for prefix in prefixes:
            if slug_without_date.startswith(prefix):
                return label
    return "Misc"


def _collect(docs_dir: Path):
    """Return {month_label: {category: [(date, title, rel_path)]}}."""
    grouped: dict[str, dict[str, list[tuple[str, str, str]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    base = docs_dir / "features_change"
    if not base.exists():
        return grouped
    pattern = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(.+)\.md$")
    for path in sorted(base.rglob("*.md")):
        match = pattern.match(path.name)
        if not match:
            continue
        year, month, day, slug = match.groups()
        date = f"{year}-{month}-{day}"
        month_label = f"{year}-{month}"
        category = _category_for(slug)
        title = _read_title(path, slug.replace("-", " ").title())
        rel = path.relative_to(docs_dir).as_posix()
        grouped[month_label][category].append((date, title, rel))
    return grouped


def _render(grouped) -> str:
    if not grouped:
        return "_(no per-feature change notes found)_\n"
    months = sorted(grouped.keys(), reverse=True)
    out: list[str] = []
    for month in months:
        cat_map = grouped[month]
        total = sum(len(v) for v in cat_map.values())
        out.append(f"## {month} ({total} notes)")
        out.append("")
        # Order categories by configured order, then Misc.
        ordered_categories = [label for label, _ in CATEGORY_PREFIXES] + ["Misc"]
        seen = set()
        for category in ordered_categories:
            if category not in cat_map:
                continue
            entries = sorted(cat_map[category], key=lambda x: x[0], reverse=True)
            seen.add(category)
            out.append(f"### {category} ({len(entries)})")
            out.append("")
            for date, title, rel in entries:
                out.append(f"- `{date}` — [{title}]({rel})")
            out.append("")
        # Any unexpected category not in our list (should be empty).
        for category in sorted(set(cat_map) - seen):
            entries = sorted(cat_map[category], key=lambda x: x[0], reverse=True)
            out.append(f"### {category} ({len(entries)})")
            out.append("")
            for date, title, rel in entries:
                out.append(f"- `{date}` — [{title}]({rel})")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def on_page_markdown(markdown, page, config, files):
    """Replace `[CHANGELOG]` on changelog.md with grouped index."""
    src = getattr(page.file, "src_path", "") or ""
    if Path(src).as_posix() != "changelog.md":
        return markdown
    if PLACEHOLDER not in markdown:
        return markdown
    grouped = _collect(Path(config["docs_dir"]))
    return markdown.replace(PLACEHOLDER, _render(grouped))
