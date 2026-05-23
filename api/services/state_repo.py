"""Compatibility facade for `api.services.state.*` subpackage.

Responsibility: Preserve the legacy `from api.services.state_repo import X` import
surface. Real logic lives in:
- `api.services.state.table_pool` — `_PooledTableClient`, table registry
- `api.services.state.job_state` — `JobState`, `canonical_job_metadata`, helpers
- `api.services.state.repository` — `JobStateRepository`, singleton getters
Edit boundaries: Do not add new logic here.
Key entry points: re-exports of every public + private symbol the old flat module
exposed.
Risky contracts: Tests that monkey-patch must target the real module
(`api.services.state.repository.JobStateRepository`) so `get_state_repo()`'s
internal lookup sees the patch.
Validation: `uv run pytest -q api/tests/test_state_repo.py`.
"""

from __future__ import annotations

import threading
from typing import Any

from azure.data.tables import TableClient, TableServiceClient

from api.services import get_credential
from api.services.state import repository as _repository
from api.services.state.job_state import (
    _JOB_SCHEMA_VERSION,
    _JOBSTATE_SUMMARY_SELECT,
    JobState,
    _basename,
    _now_iso,
    _payload_value,
    _sanitise_odata_value,
    _ulid_like,
    canonical_job_metadata,
)
from api.services.state.table_pool import (
    _ENSURED_TABLES,
    _ENSURED_TABLES_LOCK,
    _TABLE_ENDPOINT_ENV,
    _PooledTableClient,
)

_DEFAULT_REPO: Any | None = None
_DEFAULT_REPO_LOCK = threading.Lock()
_REAL_GET_STATE_REPO = _repository.get_state_repo


def _sync_patch_surface() -> None:
    """Forward legacy `state_repo` monkeypatches into the split repository module."""
    _repository.TableClient = TableClient
    _repository.TableServiceClient = TableServiceClient
    _repository.get_credential = get_credential


def JobStateRepository(table_endpoint: str | None = None) -> Any:
    """Return a repository instance while honoring split-module monkeypatches."""
    _sync_patch_surface()
    if table_endpoint is None:
        return _repository.JobStateRepository()
    return _repository.JobStateRepository(table_endpoint=table_endpoint)


def get_state_repo() -> Any:
    """Return the facade-level singleton using the patchable repository symbol."""
    if _repository.get_state_repo is not _REAL_GET_STATE_REPO:
        return _repository.get_state_repo()
    global _DEFAULT_REPO
    repo = _DEFAULT_REPO
    if repo is not None:
        return repo
    with _DEFAULT_REPO_LOCK:
        if _DEFAULT_REPO is None:
            _sync_patch_surface()
            _DEFAULT_REPO = JobStateRepository()
        return _DEFAULT_REPO


def reset_state_repo_cache() -> None:
    """Drop both facade and split-module singleton repositories."""
    global _DEFAULT_REPO
    with _DEFAULT_REPO_LOCK:
        _DEFAULT_REPO = None
    _repository.reset_state_repo_cache()

__all__ = [
    "_ENSURED_TABLES",
    "_ENSURED_TABLES_LOCK",
    "_JOBSTATE_SUMMARY_SELECT",
    "_JOB_SCHEMA_VERSION",
    "_TABLE_ENDPOINT_ENV",
    "JobState",
    "JobStateRepository",
    "TableClient",
    "TableServiceClient",
    "_PooledTableClient",
    "_basename",
    "_now_iso",
    "_payload_value",
    "_sanitise_odata_value",
    "_ulid_like",
    "canonical_job_metadata",
    "get_credential",
    "get_state_repo",
    "reset_state_repo_cache",
]
