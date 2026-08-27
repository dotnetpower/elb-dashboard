"""Durable reverse references from BLAST jobs to DB order-oracle runs.

Responsibility: Create immutable, idempotent run-reference documents before a
    BLAST job consumes an oracle pointer.
Edit boundaries: Reference writes only; oracle selection is owned by BLAST
    oracle helpers and retention/GC is owned by `oracle_retention`.
Key entry points: `create_oracle_reference`.
Risky contracts: References use `overwrite=False`; a write failure must prevent
    pointer attachment, while an existing reference is success.
Validation: `uv run pytest -q api/tests/test_oracle_references.py`.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from api.services.db.oracle_state import oracle_container
from api.services.db.order_oracle import (
    oracle_gc_marker_blob_path,
    oracle_reference_blob_path,
)

LOGGER = logging.getLogger(__name__)


class OracleRunRetiring(RuntimeError):
    """The selected run is being removed and must not be attached."""


def _is_retiring(container: Any, db_name: str, run_id: str) -> bool:
    marker = container.get_blob_client(oracle_gc_marker_blob_path(db_name, run_id))
    try:
        marker.get_blob_properties()
        return True
    except ResourceNotFoundError:
        return False


def create_oracle_reference(
    credential: Any,
    *,
    storage_account: str,
    db_name: str,
    run_id: str,
    job_id: str,
    source_version: str,
) -> str:
    path: str = oracle_reference_blob_path(db_name, run_id, job_id)
    payload = {
        "schema_version": 1,
        "db_name": db_name,
        "run_id": run_id,
        "job_id": job_id,
        "source_version": source_version,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    container = oracle_container(credential, storage_account)
    if _is_retiring(container, db_name, run_id):
        raise OracleRunRetiring(f"oracle run {run_id} is retiring")
    blob = container.get_blob_client(path)
    try:
        blob.upload_blob(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            overwrite=False,
        )
    except ResourceExistsError:
        pass
    if _is_retiring(container, db_name, run_id):
        # The marker can appear after the pre-check. Even when cleanup fails,
        # this writer MUST raise so the caller never uploads a pointer to parts
        # that retention may already be deleting. The durable reference left
        # behind is conservative: the sweeper sees it and preserves the run.
        try:
            blob.delete_blob()
        except Exception as exc:
            LOGGER.warning(
                "oracle retiring reference cleanup skipped db=%s run_id=%s reason=%s",
                db_name,
                run_id,
                type(exc).__name__,
            )
        raise OracleRunRetiring(f"oracle run {run_id} began retiring")
    return str(path)


__all__ = ["OracleRunRetiring", "create_oracle_reference"]
