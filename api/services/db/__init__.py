"""Database (BLAST DB) auxiliary service modules.

Responsibility: Group BLAST database-related service modules (ops audit, oracle, sharding).
Edit boundaries: Submodules own their logic; this package only aggregates exports.
Key entry points: `ops_audit`, `order_oracle`, `sharding` submodules.
Risky contracts: Validate DB names and shard inputs at module boundaries.
Validation: `uv run pytest -q api/tests/test_db_sharding.py api/tests/test_db_order_oracle.py`.
"""

from __future__ import annotations

__all__: list[str] = []
