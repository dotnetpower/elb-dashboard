"""pytest configuration for api/.

Responsibility: pytest configuration for api/
Edit boundaries: Keep changes scoped to this module responsibility and update nearby tests.
Key entry points: `_reset_external_jobs_cache`
Risky contracts: Keep imports lightweight and preserve existing public contracts.
Validation: `uv run pytest -q api/tests`.
"""

import os
import sys
from collections.abc import Generator
from pathlib import Path

import pytest

# Make api/ importable as `api`.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Disable the blast-db-metadata Redis pub/sub invalidation by default in
# tests. Subscribers spawn daemon threads; publishes attempt a real Redis
# connection. Individual tests that exercise the invalidation channel can
# monkeypatch this env back to false.
os.environ.setdefault("BLAST_DB_METADATA_INVALIDATE_DISABLED", "true")


@pytest.fixture(autouse=True)
def _reset_external_jobs_cache() -> Generator[None, None, None]:
    """Clear the in-memory external-OpenAPI jobs cache between every test.

    Without this, a test that mocks ``external_blast.list_jobs`` with one
    response can leak that response into a subsequent test whose mock
    expects to be the only source of truth.
    """
    from api.routes._blast_shared import _reset_external_jobs_cache as _reset
    from api.routes.blast.jobs import _reset_blast_jobs_list_cache
    from api.routes.storage.common import reset_ncbi_catalogue_cache
    from api.services.blast_db_metadata import _reset_blast_db_metadata_cache
    from api.services.k8s_monitoring import _reset_blast_status_cache
    from api.services.state_repo import reset_state_repo_cache
    from api.services.storage_data import reset_blob_service_pool

    _reset()
    _reset_blast_jobs_list_cache()
    _reset_blast_db_metadata_cache()
    _reset_blast_status_cache()
    reset_state_repo_cache()
    reset_blob_service_pool()
    reset_ncbi_catalogue_cache()
    yield
    _reset()
    _reset_blast_jobs_list_cache()
    _reset_blast_db_metadata_cache()
    _reset_blast_status_cache()
    reset_state_repo_cache()
    reset_blob_service_pool()
    reset_ncbi_catalogue_cache()
