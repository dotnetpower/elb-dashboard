#!/usr/bin/env python3
"""Render a release-notes markdown page from features_change/ files added
between two git refs.

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
import pathlib
import re
import subprocess
import sys
from collections import OrderedDict
from typing import Iterable

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
FEATURES_GLOB = "docs/features_change/**/*.md"
CC_RE = re.compile(r"^(?P<type>[a-zA-Z]+)(\([^)]+\))?(?P<bang>!?):\s*(?P<subj>.+)$")
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+)\.md$")
# Cap every `git` invocation so a wedged git process (stale lock file, an
# NFS hiccup, a half-broken submodule) raises `TimeoutExpired` instead of
# blocking the release-note rendering indefinitely. 30 s is generous for
# the largest log walk we do (full history) and small enough that an
# operator notices a stuck script in a single CI step.
_GIT_TIMEOUT_SECONDS = 30


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=REPO_ROOT, text=True, timeout=_GIT_TIMEOUT_SECONDS
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


def read_title(rel: str) -> tuple[str, str]:
    """Return (date, title) for a features_change file path."""
    path = REPO_ROOT / rel
    name = path.name
    m = DATE_RE.match(name)
    date = m.group(1) if m else ""
    slug = m.group(2) if m else path.stem
    title = ""
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("# "):
                    title = stripped[2:].strip()
                    break
        except OSError:
            pass
    if not title:
        title = slug.replace("-", " ").title()
    return date, title


def doc_rel(rel: str) -> str:
    """Path to the note from the docs/releases/ directory."""
    # rel is "docs/features_change/..."; strip "docs/" then prepend "../"
    return "../" + rel.removeprefix("docs/")


def render(version: str, range_desc: str, entries: list[dict], warnings: list[str]) -> str:
    sections: "OrderedDict[str, list[dict]]" = OrderedDict()
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
        f"description: ElasticBLAST Control Plane {version} release notes \u2014 feature-change notes that landed in this version.",
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
            lines.append(f">")
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
            from_ref = git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*.[0-9]*.[0-9]*").strip()
        except subprocess.CalledProcessError:
            from_ref = ""  # no tags yet

    range_arg = f"{from_ref}..{args.to}" if from_ref else args.to
    range_desc = f"{from_ref}..{args.to}" if from_ref else "(full history)"

    commits = commits_in_range(range_arg if from_ref else "")
    entries: list[dict] = []
    feat_fix_commit_count = 0
    for sha, subject in commits:
        kind, breaking = classify(subject)
        if kind in ("feat", "fix"):
            feat_fix_commit_count += 1
        for rel in added_files(sha):
            if not rel.startswith("docs/features_change/"):
                continue
            date, title = read_title(rel)
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

    # Deduplicate by file path (rare: same file added in two cherry-picks).
    seen: set[str] = set()
    unique: list[dict] = []
    for e in entries:
        key = e["rel"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)
    entries = unique

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
