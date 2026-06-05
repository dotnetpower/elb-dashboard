"""Upgrade target-version string helpers (release vs. commit channel).

Module summary: A single dependency-free helper that defines the two
``target_version`` string shapes the self-upgrade pipeline understands and the
conversions between them. The whole pipeline pivots on ``target_version`` (image
tag ``v<ver>``, build-arg ``APP_VERSION`` baked into ``api.__version__``, and the
reconciler's ``__version__ == target_version`` success gate), so this module is
the one place that decides what a valid target string looks like.

Responsibility: Validate / classify / construct upgrade ``target_version``
  strings for the release and commit channels.
Edit boundaries: Pure string logic only — no Azure SDK, no Storage, no network,
  no imports from sibling upgrade modules (keeps it importable everywhere
  without cycles).
Key entry points: ``is_release_version``, ``is_commit_version``,
  ``is_valid_target_version``, ``base_release``, ``make_commit_version``,
  ``commit_short_sha``, ``RELEASE_RE``, ``COMMIT_RE``.
Risky contracts: The commit form ``<base>-commit.<short_sha>`` MUST stay a
  valid Docker tag when prefixed with ``v`` (``v0.2.0-commit.a1b2c3d``) and MUST
  NOT be fed to ``packaging.version.Version`` (it is not PEP 440). Callers that
  need a semver compare use :func:`base_release` first.
Validation: ``uv run pytest -q api/tests/test_upgrade_version_target.py``.
"""

from __future__ import annotations

import re

# A release target is a bare semver, e.g. "0.4.0".
RELEASE_RE = re.compile(r"^\d+\.\d+\.\d+$")
# A commit target carries the base release plus a short commit sha, e.g.
# "0.2.0-commit.a1b2c3d". Docker-tag safe once prefixed with "v".
COMMIT_RE = re.compile(r"^(\d+\.\d+\.\d+)-commit\.([0-9a-f]{7,40})$")

# How many hex chars of the commit sha to embed in the version string. 7 is
# git's conventional short-sha length and is unambiguous for a single repo.
SHORT_SHA_LEN = 7


def is_release_version(value: str) -> bool:
    """True when ``value`` is a bare semver release target (e.g. ``0.4.0``)."""
    return bool(RELEASE_RE.match(value or ""))


def is_commit_version(value: str) -> bool:
    """True when ``value`` is a commit target (``<base>-commit.<short_sha>``)."""
    return bool(COMMIT_RE.match(value or ""))


def is_valid_target_version(value: str) -> bool:
    """True for either a release or a commit target string."""
    return is_release_version(value) or is_commit_version(value)


def base_release(value: str) -> str:
    """Return the bare-semver base of a target string.

    ``"0.2.0-commit.a1b2c3d" -> "0.2.0"`` and ``"0.4.0" -> "0.4.0"``. Returns
    the input unchanged when it is neither shape (caller decides what to do).
    Used before any ``packaging.version.Version`` compare so the non-PEP-440
    commit suffix never raises.
    """
    m = COMMIT_RE.match(value or "")
    if m:
        return m.group(1)
    return value or ""


def commit_short_sha(value: str) -> str:
    """Return the embedded short sha of a commit target, or ``""``."""
    m = COMMIT_RE.match(value or "")
    return m.group(2) if m else ""


def make_commit_version(base: str, commit_sha: str) -> str:
    """Build a commit target string from a base release + a commit sha.

    ``base`` is normalised to its bare semver (so passing an already-commit
    version re-bases cleanly). ``commit_sha`` must be at least
    :data:`SHORT_SHA_LEN` hex chars; it is lowercased and truncated to the
    short form. Raises ``ValueError`` on malformed input so a bad sha can never
    silently produce an un-cloneable target.
    """
    base_semver = base_release(base)
    if not RELEASE_RE.match(base_semver):
        raise ValueError(f"base release is not semver: {base!r}")
    sha = (commit_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        raise ValueError(f"commit sha must be 7-40 hex chars: {commit_sha!r}")
    return f"{base_semver}-commit.{sha[:SHORT_SHA_LEN]}"
