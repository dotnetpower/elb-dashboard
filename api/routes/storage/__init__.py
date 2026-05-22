"""/api/storage/*`` route package.

Responsibility: /api/storage/*`` route package
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `package imports`
Risky contracts: Never issue browser SAS URLs; local public Storage access remains debug-only
and IP-allowlisted.
Validation: `uv run pytest -q api/tests/test_storage_data.py
api/tests/test_storage_public_access.py`.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.routes.storage import local_debug as _local_debug_routes
from api.routes.storage import prepare_db as _prepare_db_routes
from api.routes.storage.local_debug import (
    storage_local_debug_open as storage_local_debug_open,
)
from api.routes.storage.local_debug import (
    storage_local_debug_status as storage_local_debug_status,
)
from api.routes.storage.prepare_db import prepare_db as prepare_db
from api.routes.storage.prepare_db import prepare_db_cancel as prepare_db_cancel
from api.services import get_credential as get_credential

router = APIRouter(prefix="/api/storage", tags=["storage"])
router.include_router(_prepare_db_routes.router)
router.include_router(_local_debug_routes.router)
