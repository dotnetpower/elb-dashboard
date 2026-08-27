"""Best-effort recursive purge of a BLAST job's Storage directories (dfs).

Responsibility: Orchestrate deletion of a deleted job's result/query directories
via the dfs recursive ``delete_directory``. Fixes the historical soft-delete-only
leak (``blast_job_delete`` flipped the Table row to ``deleted`` but never removed
the result blobs, so they accumulated forever).
Edit boundaries: Orchestration only — resolve the job's prefixes, apply the
``leaf == job_id`` safety guard, and call ``dfs_io.delete_directory_dfs``. The
low-level recursive delete + its path guard live in ``dfs_io``. Never raises:
storage cleanup is best-effort and must not block the job tombstone.
Key entry points: ``purge_job_result_storage``.
Risky contracts: Gated on ``dfs_enabled()`` (recursive delete is a dfs-only op)
AND skipped for external (``/v1/jobs``) jobs whose storage the sibling owns. Each
delete passes ``expected_leaf=job_id`` so it can only ever target the job's own
directory, never a parent date bucket. An oracle reverse reference is deleted
only after every directory operation avoided errors and its persisted path
exactly matches the path reconstructed from job/run/database identity.
Validation: ``uv run pytest -q api/tests/test_job_purge.py``.
"""

from __future__ import annotations

import logging
from typing import Any

LOGGER = logging.getLogger(__name__)


def _is_external(state: Any) -> bool:
    """True when the row originated from the OpenAPI sibling (its storage)."""
    payload = getattr(state, "payload", None)
    if isinstance(payload, dict) and isinstance(payload.get("external"), dict):
        return True
    return str(getattr(state, "owner_upn", "") or "") == "api"


def _oracle_reference_path(state: Any) -> str:
    payload = getattr(state, "payload", None)
    if not isinstance(payload, dict):
        return ""
    oracle = payload.get("db_order_oracle")
    if not isinstance(oracle, dict):
        return ""
    db_name = str(oracle.get("db_name") or "")
    run_id = str(oracle.get("oracle_run_id") or "")
    job_id = str(getattr(state, "job_id", "") or "")
    actual = str(oracle.get("reference_blob") or "")
    if not all((db_name, run_id, job_id, actual)):
        return ""
    try:
        from api.services.db.order_oracle import oracle_reference_blob_path

        expected = oracle_reference_blob_path(db_name, run_id, job_id)
    except ValueError:
        return ""
    return actual if actual == expected else ""


def _delete_oracle_reference(
    credential: Any,
    *,
    storage_account: str,
    path: str,
) -> bool:
    if not path:
        return False
    from azure.core.exceptions import ResourceNotFoundError

    from api.services.db.oracle_state import oracle_container

    try:
        oracle_container(credential, storage_account).get_blob_client(path).delete_blob()
        return True
    except ResourceNotFoundError:
        return False
    except Exception as exc:
        LOGGER.warning(
            "oracle reverse reference purge skipped path=%s reason=%s",
            path,
            type(exc).__name__,
        )
        return False


def purge_job_result_storage(state: Any) -> dict[str, Any]:
    """Recursively delete a job's result/query directories. Best-effort.

    Returns ``{"purged": bool, "deleted": [...], "reason": str}``. A no-op
    (``purged=False``) when the date/dfs feature is off, the job is external, or
    no storage account is known — so the legacy soft-delete-only behaviour is
    preserved with the flag off. Never raises.
    """
    try:
        from api.services.storage.dfs_client_pool import dfs_enabled

        if not dfs_enabled():
            return {"purged": False, "deleted": [], "reason": "dfs_disabled"}
        if _is_external(state):
            # External jobs run on the sibling's cluster; it owns their storage.
            return {"purged": False, "deleted": [], "reason": "external_job"}
        job_id = str(getattr(state, "job_id", "") or "")
        account = str(getattr(state, "storage_account", "") or "")
        if not job_id or not account:
            return {"purged": False, "deleted": [], "reason": "missing_scope"}

        from api.services import get_credential
        from api.services.storage.dfs_io import delete_directory_dfs
        from api.services.storage.job_prefix import resolve_results_prefix

        cred = get_credential()
        results_prefix = resolve_results_prefix(job_id, state=state)
        targets: list[tuple[str, str]] = [
            ("results", results_prefix),
            ("queries", f"{job_id}/"),
            ("queries", f"uploads/{job_id}/"),
        ]
        deleted: list[str] = []
        errors: list[str] = []
        for container, path in targets:
            try:
                if delete_directory_dfs(cred, account, container, path, expected_leaf=job_id):
                    deleted.append(f"{container}/{path.rstrip('/')}")
            except Exception as exc:
                errors.append(f"{container}/{path.rstrip('/')}")
                LOGGER.warning(
                    "job purge skipped container=%s job_id=%s: %s",
                    container,
                    job_id,
                    type(exc).__name__,
                )
        reference_deleted = False
        if not errors:
            reference_path = _oracle_reference_path(state)
            if reference_path:
                reference_deleted = _delete_oracle_reference(
                    cred,
                    storage_account=account,
                    path=reference_path,
                )
        return {
            "purged": True,
            "deleted": deleted,
            "reference_deleted": reference_deleted,
            "reason": "partial" if errors else "ok",
        }
    except Exception as exc:
        # A purge failure must never block the job tombstone.
        LOGGER.warning(
            "job purge failed job_id=%s: %s",
            getattr(state, "job_id", "?"),
            type(exc).__name__,
        )
        return {"purged": False, "deleted": [], "reason": "error"}
