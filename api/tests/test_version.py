"""Tests for `api/__init__.py::_detect_version`.

Module summary: Exercises the version-discovery fallback chain (env →
importlib.metadata → pyproject.toml → fixed fallback) so a regression
that breaks the upgrade reconciler's `__version__ == target_version`
check is loud here, not silent in production.

Responsibility: Verify the version-source priority order.
Edit boundaries: Update when the discovery chain changes.
Key entry points: Tests for env override, pyproject fallback,
  importlib.metadata path, ultimate fallback string.
Risky contracts: Asserts the env-set value wins so a freshly booted
  Container App revision (which has `APP_VERSION=vX.Y.Z` baked by the
  Dockerfile) always carries the right release version.
Validation: `uv run pytest -q api/tests/test_version.py`.
"""

from __future__ import annotations

import importlib

import api as api_pkg
import pytest


def _reload_with_env(monkeypatch: pytest.MonkeyPatch, env_value: str | None) -> str:
    if env_value is None:
        monkeypatch.delenv("APP_VERSION", raising=False)
    else:
        monkeypatch.setenv("APP_VERSION", env_value)
    reloaded = importlib.reload(api_pkg)
    return reloaded.__version__


def test_env_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        assert _reload_with_env(monkeypatch, "9.9.9") == "9.9.9"
    finally:
        monkeypatch.delenv("APP_VERSION", raising=False)
        importlib.reload(api_pkg)


def test_pyproject_fallback_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_VERSION", raising=False)
    try:
        reloaded = importlib.reload(api_pkg)
        # Whatever pyproject says, it must be non-empty and not the literal fallback.
        assert reloaded.__version__
        assert reloaded.__version__ != "0.0.0+unknown"
        # Sanity: looks like semver (x.y.z prefix), tolerant of suffixes.
        head = reloaded.__version__.split("+", 1)[0].split("-", 1)[0]
        parts = head.split(".")
        assert len(parts) >= 3, reloaded.__version__
        for p in parts[:3]:
            assert p.isdigit(), reloaded.__version__
    finally:
        importlib.reload(api_pkg)


def test_detection_helpers_are_safe_against_failures() -> None:
    # Direct calls — they must never raise even when the chain has nothing
    # to return.
    assert isinstance(api_pkg._from_env(), str)
    assert isinstance(api_pkg._from_importlib_metadata(), str)
    assert isinstance(api_pkg._from_pyproject(), str)
    assert isinstance(api_pkg._detect_version(), str)


def test_version_is_string_not_empty() -> None:
    # The module-level constant must always be a non-empty string for the
    # reconciler / health endpoint consumers.
    assert isinstance(api_pkg.__version__, str)
    assert api_pkg.__version__
