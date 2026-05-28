"""Storage and warmup Celery tasks for prepared BLAST databases (package facade).

Responsibility: Re-export the public task entry points and helpers that external callers
    (`api.routes`, `api.services.auto_warmup_reconcile`, tests, beat schedules) import
    from `api.tasks.storage`. The actual task and helper code lives in sibling modules.
Edit boundaries: Imports and re-exports only. Add new tasks in dedicated sibling modules
    and re-export them here; do not grow this file with logic.
Key entry points: `warmup_database`, `check_database_updates`, `reconcile_auto_warmup`,
    plus the helpers and constants exported below.
Risky contracts: Several tests monkeypatch attributes on this package directly
    (`api.tasks.storage.get_credential`, `_autowarmup_inflight_acquire`, `_update_state`,
    `_record_task_progress`). These names must remain importable from the package and
    listed in `__all__` (see `api/tests/test_tasks_facade_contract.py`).
Validation: `uv run pytest -q api/tests/test_auto_warmup.py api/tests/test_warmup_jobs.py
    api/tests/test_warmup_route.py api/tests/test_tasks_facade_contract.py`.
"""

from __future__ import annotations

from api.services import get_credential
from api.services.auto_warmup_reconcile import (
    autowarmup_inflight_acquire as _autowarmup_inflight_acquire,
)
from api.services.warmup.task_planning import (
    build_elb_image as _build_elb_image,
)
from api.services.warmup.task_planning import (
    program_to_mol_type as _program_to_mol_type,
)
from api.services.warmup.task_planning import (
    select_warmup_shard_count as _select_warmup_shard_count,
)
from api.tasks.storage.helpers import (
    BLAST_DATABASES,
)
from api.tasks.storage.helpers import (
    now_iso as _now_iso,
)
from api.tasks.storage.helpers import (
    publish_db_metadata_invalidate as _publish_db_metadata_invalidate,
)
from api.tasks.storage.helpers import (
    record_task_progress as _record_task_progress,
)
from api.tasks.storage.helpers import (
    update_state as _update_state,
)
from api.tasks.storage.helpers import (
    wait_for_warmup_jobs as _wait_for_warmup_jobs,
)
from api.tasks.storage.prepare_db_via_aks import prepare_db_via_aks
from api.tasks.storage.reconcile import reconcile_auto_warmup
from api.tasks.storage.update_check import check_database_updates
from api.tasks.storage.warmup import warmup_database

__all__ = (
    "BLAST_DATABASES",
    "_autowarmup_inflight_acquire",
    "_build_elb_image",
    "_now_iso",
    "_program_to_mol_type",
    "_publish_db_metadata_invalidate",
    "_record_task_progress",
    "_select_warmup_shard_count",
    "_update_state",
    "_wait_for_warmup_jobs",
    "check_database_updates",
    "get_credential",
    "prepare_db_via_aks",
    "reconcile_auto_warmup",
    "warmup_database",
)
