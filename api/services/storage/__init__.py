"""Azure Storage service modules (blob I/O, endpoints, networking, usage cache).

Responsibility: Group Storage-related service modules under one namespace.
Edit boundaries: Submodules own their logic; this package only aggregates exports.
Key entry points: `data`, `endpoint`, `network`, `public_access`, `url_validation`, `usage_cache`.
Risky contracts: Never issue browser SAS URLs; `publicNetworkAccess` stays Disabled in production.
Validation: `uv run pytest -q api/tests/test_storage_data.py api/tests/test_storage_network.py`.
"""

from __future__ import annotations

__all__: list[str] = []
