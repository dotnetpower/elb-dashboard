"""pytest configuration for api/.

Responsibility: pytest configuration for api/
Edit boundaries: Keep changes scoped to this module responsibility and update nearby tests.
Key entry points: `_env_baseline`, `_reset_external_jobs_cache`
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
def _env_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Force a clean environment for every test.

    Two motivations, both seen in recurring failures:

    * ``AZURE_TABLE_ENDPOINT`` — when set (e.g. by ``scripts/dev/local-run.sh``)
      the state-repo tries to write to a real Azure Table during unit tests,
      causing ``AzureError: tenant must be specified`` warnings, slow
      retries, and the cross-test state pollution we saw in the 2026-05-22
      facade-refactor failure cascade. Tests that need a Table backend
      monkeypatch it back inside the test body — that override wins because
      ``monkeypatch.setenv`` inside the test body re-applies AFTER this
      autouse setup.
    * ``ELB_LOCAL_STATE_DIR`` — when unset the state-repo's local JSON
      fallback writes to the repo root. Default it to a per-test temp dir
      so stray writes never pollute the workspace and concurrent runs do
      not collide on the same file.
    """
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path_factory.mktemp("elb_state")))


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
    from api.services.auto_warmup import _reset_autowarmup_table_pool
    from api.services.blast.db_metadata import _reset_blast_db_metadata_cache
    from api.services.httpx_pool import close_all_clients as _reset_httpx_pool
    from api.services.job_artifacts import _reset_artifact_table_pool
    from api.services.k8s.monitoring import (
        _reset_blast_status_cache,
        reset_k8s_credential_cache,
        reset_k8s_session_pool,
    )
    from api.services.redis_clients import reset_redis_clients
    from api.services.state_repo import reset_state_repo_cache
    from api.services.storage.data import reset_blob_service_pool

    _reset()
    _reset_blast_jobs_list_cache()
    _reset_blast_db_metadata_cache()
    _reset_blast_status_cache()
    reset_state_repo_cache()
    reset_blob_service_pool()
    reset_ncbi_catalogue_cache()
    reset_k8s_credential_cache()
    reset_k8s_session_pool()
    reset_redis_clients()
    _reset_artifact_table_pool()
    _reset_autowarmup_table_pool()
    _reset_httpx_pool()
    yield
    _reset()
    _reset_blast_jobs_list_cache()
    _reset_blast_db_metadata_cache()
    _reset_blast_status_cache()
    reset_state_repo_cache()
    reset_blob_service_pool()
    reset_ncbi_catalogue_cache()
    reset_k8s_credential_cache()
    reset_k8s_session_pool()
    reset_redis_clients()
    _reset_artifact_table_pool()
    _reset_autowarmup_table_pool()
    _reset_httpx_pool()
