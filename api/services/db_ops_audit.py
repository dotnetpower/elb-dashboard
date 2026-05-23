"""Compatibility wrapper for `api.services.db.ops_audit`.

Responsibility: Re-export `api.services.db.ops_audit` at the legacy flat path.
Edit boundaries: Implementation lives in `api.services.db.ops_audit`; do not add logic here.
Key entry points: Module import side effects and constants.
Risky contracts: Keep `__all__` in sync with the underlying module's public surface.
Validation: `uv run pytest -q api/tests/test_prepare_db_routes.py`.
"""

from api.services.db.ops_audit import (
    _job_id,
    record_db_op,
    record_db_op_event,
)

__all__ = [
    "_job_id",
    "record_db_op",
    "record_db_op_event",
]
