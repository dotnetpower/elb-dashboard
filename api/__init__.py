"""FastAPI control-plane API for ElasticBLAST on Azure.

Responsibility: FastAPI control-plane API for ElasticBLAST on Azure
Edit boundaries: Keep changes scoped to this module responsibility and update nearby tests.
Key entry points: `__all__`
Risky contracts: Keep imports lightweight and preserve existing public contracts.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.0.1"
