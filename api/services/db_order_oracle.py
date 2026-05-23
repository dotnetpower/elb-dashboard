"""Compatibility wrapper for `api.services.db.order_oracle`.

Responsibility: Re-export `api.services.db.order_oracle` at the legacy flat path.
Edit boundaries: Implementation lives in `api.services.db.order_oracle`; do not add logic here.
Key entry points: Module import side effects and constants.
Risky contracts: Keep `__all__` in sync with the underlying module's public surface.
Validation: `uv run pytest -q api/tests/test_db_order_oracle.py`.
"""

from api.services.db.order_oracle import (
    ORACLE_PARTS_DIR,
    ORACLE_PREFIX_ROOT,
    ORACLE_STATUS_BLOB_NAME,
    DbOrderOracleJobPlan,
    build_db_order_oracle_job_plan,
    oracle_part_blob_path,
    oracle_part_url,
    oracle_status_blob_path,
)

__all__ = [
    "ORACLE_PARTS_DIR",
    "ORACLE_PREFIX_ROOT",
    "ORACLE_STATUS_BLOB_NAME",
    "DbOrderOracleJobPlan",
    "build_db_order_oracle_job_plan",
    "oracle_part_blob_path",
    "oracle_part_url",
    "oracle_status_blob_path",
]
