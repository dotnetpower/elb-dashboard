"""FastAPI control-plane API for ElasticBLAST on Azure.

Responsibility: FastAPI control-plane API for ElasticBLAST on Azure
Edit boundaries: Keep changes scoped to this module responsibility and update nearby tests.
Key entry points: `__all__`, `__version__`
Risky contracts: `__version__` is consumed by `/api/health`, the upgrade
  reconciler's `__version__ == target_version` check, and the SPA stamp.
  It MUST reflect the release that built the running image — never
  hard-code it. Discovery order: `APP_VERSION` env (set by `api/Dockerfile`
  ARG/ENV at build time) → `importlib.metadata.version("elb-dashboard")`
  (when the package is pip-installed) → `pyproject.toml` parse (developer
  source-mount mode) → "0.0.0+unknown" fallback.
Validation: `uv run pytest -q api/tests/test_smoke.py api/tests/test_version.py`.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["__version__"]


def _from_env() -> str:
    """Highest-priority source: APP_VERSION env baked into the image."""
    return os.environ.get("APP_VERSION", "").strip()


def _from_importlib_metadata() -> str:
    try:
        from importlib.metadata import version

        return version("elb-dashboard")
    except Exception:
        return ""


def _from_pyproject() -> str:
    """Developer-mode fallback: parse pyproject.toml from the repo root."""
    try:
        import tomllib  # py311+
    except ImportError:  # pragma: no cover - py<3.11 unsupported
        return ""
    here = Path(__file__).resolve()
    for parent in (here.parent.parent, here.parent.parent.parent):
        candidate = parent / "pyproject.toml"
        if not candidate.is_file():
            continue
        try:
            with candidate.open("rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        value = data.get("project", {}).get("version")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _detect_version() -> str:
    for source in (_from_env, _from_importlib_metadata, _from_pyproject):
        try:
            value = source()
        except Exception:
            value = ""
        if value:
            return value
    return "0.0.0+unknown"


__version__ = _detect_version()
