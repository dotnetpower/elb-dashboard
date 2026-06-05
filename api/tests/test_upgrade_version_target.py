"""Tests for the upgrade target-version string helpers.

Module summary: Exercises the release/commit classification, base-release
reduction, and commit-version construction in
`api.services.upgrade.version_target`.

Responsibility: Verify the version-string contract the whole upgrade
  pipeline pivots on stays correct.
Edit boundaries: Update when the commit version format changes.
Key entry points: Tests for is_release/is_commit/is_valid, base_release,
  make_commit_version, commit_short_sha.
Risky contracts: The commit form must stay Docker-tag safe (no chars
  outside `[A-Za-z0-9._-]` once prefixed with `v`).
Validation: `uv run pytest -q api/tests/test_upgrade_version_target.py`.
"""

from __future__ import annotations

import re

import pytest
from api.services.upgrade import version_target as vt


def test_release_classification() -> None:
    assert vt.is_release_version("0.4.0")
    assert not vt.is_release_version("0.4")
    assert not vt.is_release_version("0.2.0-commit.a1b2c3d")
    assert not vt.is_release_version("")


def test_commit_classification() -> None:
    assert vt.is_commit_version("0.2.0-commit.a1b2c3d")
    assert vt.is_commit_version("0.2.0-commit." + "a" * 40)
    assert not vt.is_commit_version("0.2.0-commit.ZZZ")
    assert not vt.is_commit_version("0.2.0")
    assert not vt.is_commit_version("0.2.0-commit.abc")  # < 7 hex


def test_is_valid_target_version() -> None:
    assert vt.is_valid_target_version("0.4.0")
    assert vt.is_valid_target_version("0.2.0-commit.a1b2c3d")
    assert not vt.is_valid_target_version("garbage")


def test_base_release() -> None:
    assert vt.base_release("0.2.0-commit.a1b2c3d") == "0.2.0"
    assert vt.base_release("0.4.0") == "0.4.0"
    assert vt.base_release("weird") == "weird"


def test_commit_short_sha() -> None:
    assert vt.commit_short_sha("0.2.0-commit.a1b2c3d") == "a1b2c3d"
    assert vt.commit_short_sha("0.4.0") == ""


def test_make_commit_version_truncates_and_lowercases() -> None:
    out = vt.make_commit_version("0.2.0", "A1B2C3D4E5F6")
    assert out == "0.2.0-commit.a1b2c3d"
    # Re-basing from an existing commit version uses its base.
    assert vt.make_commit_version("0.2.0-commit.deadbee", "f" * 40) == "0.2.0-commit.fffffff"


def test_make_commit_version_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        vt.make_commit_version("not-semver", "a" * 40)
    with pytest.raises(ValueError):
        vt.make_commit_version("0.2.0", "xyz")


def test_commit_version_is_docker_tag_safe() -> None:
    v = vt.make_commit_version("0.2.0", "a1b2c3d")
    tag = f"v{v}"
    # Docker tag: [A-Za-z0-9_][A-Za-z0-9._-]{0,127}
    assert re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}", tag)
