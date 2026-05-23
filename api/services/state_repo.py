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
from api.services.state.repository import (
    JobStateRepository,
    get_state_repo,
    reset_state_repo_cache,
)
from api.services.state.table_pool import (
    _ENSURED_TABLES,
    _ENSURED_TABLES_LOCK,
    _TABLE_ENDPOINT_ENV,
    _PooledTableClient,
)

__all__ = [
    "_ENSURED_TABLES",
    "_ENSURED_TABLES_LOCK",
    "_JOB_SCHEMA_VERSION",
    "_JOBSTATE_SUMMARY_SELECT",
    "_TABLE_ENDPOINT_ENV",
    "JobState",
    "JobStateRepository",
    "_PooledTableClient",
    "_basename",
    "_now_iso",
    "_payload_value",
    "_sanitise_odata_value",
    "_ulid_like",
    "canonical_job_metadata",
    "get_state_repo",
    "reset_state_repo_cache",
]
