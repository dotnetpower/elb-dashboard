"""Add `tags:` frontmatter to ElasticBLAST docs.

One-off helper (PR B). Idempotent: re-running is a no-op if `tags:` already
appears in the frontmatter. Run from repo root:

    uv run python scripts/docs/apply_tags.py

Tag canon (also documented in docs/tags.md):

  setup, architecture, auth, blast, terminal, infra, ui, agent, security,
  release, user-guide, operate, contributor, research, overview
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "docs"

# Map: relative-to-docs path -> list of tags.
TAG_MAP: dict[str, list[str]] = {
    "index.md": ["overview"],
    "get-started.md": ["setup"],
    "joining-existing-deployment.md": ["setup"],
    "troubleshooting.md": ["setup"],
    "changelog.md": ["release"],
    "releases/index.md": ["release"],
    "releases/v0.2.0.md": ["release"],
    "releases/v0.1.0.md": ["release"],
    "releases/unreleased.md": ["release"],
    "user-guide/index.md": ["user-guide"],
    "user-guide/dashboard.md": ["user-guide", "ui"],
    "user-guide/new-search.md": ["user-guide", "blast"],
    "user-guide/jobs.md": ["user-guide", "blast"],
    "user-guide/results.md": ["user-guide", "blast"],
    "user-guide/api-reference.md": ["user-guide"],
    "user-guide/ui-preview.md": ["user-guide", "ui"],
    "user-guide/terminal.md": ["user-guide", "terminal"],
    "user-guide/upgrades.md": ["user-guide"],
    "operate/index.md": ["operate"],
    "operate/cli-upgrade.md": ["operate", "infra"],
    "deployment-reference.md": ["operate", "infra"],
    "architecture/index.md": ["architecture"],
    "architecture/high-level.md": ["architecture"],
    "architecture/container-apps.md": ["architecture", "infra"],
    "architecture/authentication.md": ["architecture", "auth", "security"],
    "research/blast-searchsp-discovery.md": ["research", "blast"],
    "research/web-blast-compatibility-plan.md": ["research", "blast"],
    "contributor-guide/index.md": ["contributor"],
    "contributor-guide/screenshot-workflow.md": ["contributor"],
    "copilot/index.md": ["agent"],
    "copilot/codebase-map.md": ["agent"],
    "copilot/repo-layout.md": ["agent"],
    "copilot/auth-flow.md": ["agent", "auth"],
    "copilot/browser-terminal.md": ["agent", "terminal"],
    "copilot/resource-plane.md": ["agent"],
    "copilot/monitoring-ui.md": ["agent", "ui"],
    "copilot/glass-ui.md": ["agent", "ui"],
    "copilot/version-management.md": ["agent"],
    "copilot/security-audit-followup.md": ["agent", "security"],
}


def add_tags(path: Path, tags: list[str]) -> str:
    text = path.read_text(encoding="utf-8")
    block = "tags:\n" + "".join(f"  - {t}\n" for t in tags)

    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end == -1:
            return "no-fm-close"
        front = text[4:end]
        rest = text[end + 5 :]
        if "\ntags:" in "\n" + front or front.startswith("tags:"):
            return "already-tagged"
        new_front = front.rstrip() + "\n" + block
        path.write_text(f"---\n{new_front}---\n{rest}", encoding="utf-8")
        return "updated"

    path.write_text(f"---\n{block}---\n\n{text}", encoding="utf-8")
    return "frontmatter-created"


def main() -> int:
    if not ROOT.exists():
        print(f"missing docs/: {ROOT}", file=sys.stderr)
        return 1
    counts: dict[str, int] = {}
    for rel, tags in TAG_MAP.items():
        path = ROOT / rel
        if not path.exists():
            counts["missing"] = counts.get("missing", 0) + 1
            print(f"MISSING  {rel}")
            continue
        result = add_tags(path, tags)
        counts[result] = counts.get(result, 0) + 1
        print(f"{result:18s} {rel}")
    print("---")
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
