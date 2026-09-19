#!/usr/bin/env python3
"""Render release notes from surviving feature-change files between git refs.

Responsibility: Build categorized release-note Markdown from change notes added
    in a revision range and still present in the target ref.
Edit boundaries: Git history/tree reads and Markdown rendering only; never
    create tags, commits, or release objects.
Key entry points: ``release_range``, ``collect_entries``, ``render``, ``main``.
Risky contracts: Respect the explicit target ref, exclude deleted/pre-rename
    paths that would become broken links, and keep mismatch warnings visible.
Validation: ``uv run pytest -q api/tests/test_render_release_notes.py``.

Usage:
  render_release_notes.py --version v0.2.0 --from v0.1.0 --to HEAD \\
      --out docs/releases/v0.2.0.md
  render_release_notes.py --version Unreleased --from "" --to HEAD \\
      --out docs/releases/unreleased.md  # "" = since last v* tag

Behaviour:
  * Groups entries into Breaking / Features / Fixes / Other sections based
    on the Conventional Commit prefix of the commit that added the note.
  * Detects renames (`-M --find-renames`) so moving a note between months
    doesn't drop it.
  * Appends the short SHA of the adding commit to every entry.
  * Emits a mismatch warning when the number of `feat:`/`fix:` commits in
    the range exceeds the number of new features_change files (charter
    §13 expects every behaviour-changing commit to carry a note).
  * Always exits 0; warnings go to stderr and into the rendered page.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
from collections import OrderedDict

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
FEATURES_GLOB = "docs/features_change/**/*.md"
CC_RE = re.compile(r"^(?P<type>[a-zA-Z]+)(\([^)]+\))?(?P<bang>!?):\s*(?P<subj>.+)$")
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+)\.md$")
H1_RE = re.compile(r"^ {0,3}#\s+(.+?)\s*$")
FENCE_RE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})")
# Cap every `git` invocation so a wedged git process (stale lock file, an
# NFS hiccup, a half-broken submodule) raises `TimeoutExpired` instead of
# blocking the release-note rendering indefinitely. 30 s is generous for
# the largest log walk we do (full history) and small enough that an
# operator notices a stuck script in a single CI step.
_GIT_TIMEOUT_SECONDS = 30


def git(*args: str) -> str:
    # Trusted invocation: `git` is a system binary on every dev / CI host,
    # `args` come from this script's own callers (no untrusted input).
    return subprocess.check_output(  # noqa: S603 - trusted git CLI
        ["git", *args],  # noqa: S607 - rely on PATH-resolved git
        cwd=REPO_ROOT,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def commits_in_range(range_arg: str | None) -> list[tuple[str, str]]:
    """Return [(sha, subject)] for commits in the range, oldest first.

    Empty range_arg means full history.
    """
    args = ["log", "--reverse", "--format=%H%x09%s"]
    if range_arg:
        args.insert(1, range_arg)
    out = git(*args)
    rows: list[tuple[str, str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, _, subj = line.partition("\t")
        rows.append((sha, subj))
    return rows


def added_files(sha: str) -> list[str]:
    """Files added (A) or copied/renamed-into (R/C target side) by `sha`
    under the features_change tree.
    """
    out = git(
        "show",
        sha,
        "--diff-filter=ARC",
        "-M",
        "--find-renames",
        "--name-only",
        "--pretty=format:",
        "--",
        FEATURES_GLOB,
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def path_exists_at_ref(rel: str, ref: str) -> bool:
    """Return whether ``rel`` exists in the target release tree.

    A note can be added and later deleted or renamed within one release range.
    Linking its historical add path produces a broken release page even though
    the adding commit is still in the range. Filter against the target tree;
    rename targets are discovered independently by ``added_files`` on the
    rename commit.
    """
    try:
        subprocess.check_call(  # noqa: S603 - trusted git CLI
            ["git", "cat-file", "-e", f"{ref}:{rel}"],  # noqa: S607 - PATH git
            cwd=REPO_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError:
        return False
    return True


def classify(subject: str) -> tuple[str, bool]:
    """Return (kind, breaking) where kind in {feat,fix,other}."""
    m = CC_RE.match(subject.strip())
    if not m:
        return "other", False
    typ = m["type"].lower()
    breaking = m["bang"] == "!"
    if typ == "feat":
        return "feat", breaking
    if typ == "fix":
        return "fix", breaking
    return "other", breaking


def _frontmatter_title(text: str) -> str:
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---\n", 4)
    if end == -1:
        return ""
    for raw in text[4:end].splitlines():
        if raw.startswith((" ", "\t")):
            continue
        key, separator, value = raw.partition(":")
        value = value.strip()
        if separator and key.strip() == "title" and value:
            if value.startswith('"') and value.endswith('"'):
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError:
                    pass
                else:
                    return parsed if isinstance(parsed, str) else value
            if value.startswith("'") and value.endswith("'"):
                return value[1:-1].replace("''", "'")
            return value
    return ""


def _first_h1_outside_fences(text: str) -> str:
    fence_character = ""
    fence_length = 0
    for line in text.splitlines():
        fence = FENCE_RE.match(line)
        if fence is not None:
            marker = fence.group("marker")
            if not fence_character:
                fence_character = marker[0]
                fence_length = len(marker)
            elif marker[0] == fence_character and len(marker) >= fence_length:
                fence_character = ""
                fence_length = 0
            continue
        if fence_character:
            continue
        heading = H1_RE.match(line)
        if heading is not None:
            return heading.group(1).strip()
    return ""


def read_title(rel: str, target_ref: str | None = None) -> tuple[str, str]:
    """Return (date, title) for a features_change file path."""
    path = REPO_ROOT / rel
    name = path.name
    m = DATE_RE.match(name)
    date = m.group(1) if m else ""
    slug = m.group(2) if m else path.stem
    title = ""
    try:
        if target_ref:
            text = subprocess.check_output(  # noqa: S603 - trusted git CLI
                ["git", "show", f"{target_ref}:{rel}"],  # noqa: S607 - PATH git
                cwd=REPO_ROOT,
                text=True,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        elif path.exists():
            text = path.read_text(encoding="utf-8")
        else:
            text = ""
        title = _frontmatter_title(text) or _first_h1_outside_fences(text)
    except (OSError, subprocess.CalledProcessError):
        pass
    if not title:
        title = slug.replace("-", " ").title()
    return date, title


def doc_rel(rel: str) -> str:
    """Path to the note from the docs/releases/ directory."""
    # rel is "docs/features_change/..."; strip "docs/" then prepend "../"
    return "../" + rel.removeprefix("docs/")


def release_range(from_ref: str, to_ref: str) -> tuple[str, str]:
    """Return the git revision argument and human-readable range label."""
    if from_ref:
        value = f"{from_ref}..{to_ref}"
        return value, value
    return to_ref, f"(repository root)..{to_ref}"


def collect_entries(commits: list[tuple[str, str]], target_ref: str) -> tuple[list[dict], int]:
    """Collect unique change-note entries that survive in ``target_ref``."""
    entries: list[dict] = []
    feat_fix_commit_count = 0
    for sha, subject in commits:
        kind, breaking = classify(subject)
        if kind in ("feat", "fix"):
            feat_fix_commit_count += 1
        for rel in added_files(sha):
            if not rel.startswith("docs/features_change/"):
                continue
            if not path_exists_at_ref(rel, target_ref):
                continue
            date, title = read_title(rel, target_ref)
            entries.append(
                {
                    "sha": sha,
                    "subject": subject,
                    "kind": kind,
                    "breaking": breaking,
                    "date": date,
                    "title": title,
                    "rel": doc_rel(rel),
                }
            )

    seen: set[str] = set()
    unique: list[dict] = []
    for entry in entries:
        key = entry["rel"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique, feat_fix_commit_count


def render(version: str, range_desc: str, entries: list[dict], warnings: list[str]) -> str:
    sections: OrderedDict[str, list[dict]] = OrderedDict()
    for label in ("Breaking changes", "Features", "Fixes", "Other"):
        sections[label] = []
    for e in entries:
        if e["breaking"]:
            sections["Breaking changes"].append(e)
        elif e["kind"] == "feat":
            sections["Features"].append(e)
        elif e["kind"] == "fix":
            sections["Fixes"].append(e)
        else:
            sections["Other"].append(e)

    lines = [
        "---",
        f"title: {version}",
        (
            f"description: ElasticBLAST Control Plane {version} release notes \u2014 "
            "feature-change notes that landed in this version."
        ),
        "tags:",
        "  - release",
        "---",
        "",
        f"# {version}",
        "",
    ]
    lines.append(f"Feature-change notes added in `{range_desc}`.")
    lines.append("")
    if warnings:
        lines.append("> **Warnings**")
        for w in warnings:
            lines.append(">")
            lines.append(f"> - {w}")
        lines.append("")
    if not entries:
        lines.append("_No new feature-change notes in this range._")
        lines.append("")
    else:
        lines.append(f"**Count:** {len(entries)}")
        lines.append("")
        for label, items in sections.items():
            if not items:
                continue
            lines.append(f"## {label}")
            lines.append("")
            for e in sorted(items, key=lambda x: x["date"], reverse=True):
                prefix = f"`{e['date']}` — " if e["date"] else ""
                sha = e["sha"][:7]
                lines.append(
                    f"- {prefix}[{e['title']}]({e['rel']}) "
                    f"([`{sha}`](https://github.com/dotnetpower/elb-dashboard/commit/{e['sha']}))"
                )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", required=True)
    ap.add_argument("--from", dest="from_ref", default="", help="empty = full history")
    ap.add_argument("--to", default="HEAD")
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--auto-from-last-tag",
        action="store_true",
        help="if --from is empty, resolve it to `git describe --tags --abbrev=0`",
    )
    args = ap.parse_args()

    from_ref = args.from_ref
    if not from_ref and args.auto_from_last_tag:
        try:
            from_ref = git(
                "describe",
                "--tags",
                "--abbrev=0",
                "--match",
                "v[0-9]*.[0-9]*.[0-9]*",
            ).strip()
        except subprocess.CalledProcessError:
            from_ref = ""  # no tags yet

    range_arg, range_desc = release_range(from_ref, args.to)

    commits = commits_in_range(range_arg)
    entries, feat_fix_commit_count = collect_entries(commits, args.to)

    warnings: list[str] = []
    note_count = len(entries)
    if feat_fix_commit_count > note_count:
        gap = feat_fix_commit_count - note_count
        warnings.append(
            f"{feat_fix_commit_count} `feat:`/`fix:` commits in range vs {note_count} "
            f"new feature-change notes — {gap} commit(s) may be missing a note "
            "(charter §13)."
        )
        print(f"[render-release-notes] WARNING: {warnings[-1]}", file=sys.stderr)

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(args.version, range_desc, entries, warnings), encoding="utf-8")
    print(f"[render-release-notes] wrote {out_path} ({note_count} notes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
