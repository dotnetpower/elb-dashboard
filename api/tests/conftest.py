"""pytest configuration for api/tests/.

Responsibility: pytest fixtures + env baseline for the api/ backend suite.
Edit boundaries: Keep changes scoped to test setup; do not import heavy
    modules at top level (slows collection across xdist workers).
Key entry points: `_env_baseline`, `_reset_external_jobs_cache`
Risky contracts: The autouse fixtures reset process-level singletons used by
    routes/services/tasks. xdist workers are separate processes, so each
    worker's resets are isolated. Tests inside the same worker rely on the
    reset to avoid cross-test pollution.
Validation: `uv run pytest -q api/tests`.
"""

import os
from collections.abc import Generator

import pytest

# Disable the blast-db-metadata Redis pub/sub invalidation by default in
# tests. Subscribers spawn daemon threads; publishes attempt a real Redis
# connection. Individual tests that exercise the invalidation channel can
# monkeypatch this env back to false.
os.environ.setdefault("BLAST_DB_METADATA_INVALIDATE_DISABLED", "true")
# Disable submit retry sleeps in tests. The retry path is exercised by a
# dedicated retry test that re-imports the module with the env unset.
# Without this, every test that mocks ``submit_job`` to raise a transport
# error pays multiple seconds of real backoff sleep.
os.environ.setdefault("OPENAPI_SUBMIT_MAX_RETRIES", "0")


@pytest.fixture(autouse=True)
def _env_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Force clean per-test environment state."""
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    # Tests that need dev auth bypass opt in explicitly; ambient CI/local env must not leak.
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
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

    # Per-token rate-limit middleware keeps in-process counters; reset
    # between tests so a burst-test doesn't leak its sliding window into
    # the next test's first request.
    try:
        from api.app.openapi_rate_limit import reset_openapi_rate_limit_state
    except Exception:
        reset_openapi_rate_limit_state = lambda: None  # type: ignore[assignment]  # noqa: E731

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
    reset_openapi_rate_limit_state()
    yield
    _reset()
    _reset_blast_jobs_list_cache()
    _reset_blast_db_metadata_cache()
    _reset_blast_status_cache()
    reset_state_repo_cache()
    reset_blob_service_pool()
    reset_ncbi_catalogue_cache()
    reset_k8s_session_pool()
    reset_k8s_credential_cache()
    reset_redis_clients()
    _reset_artifact_table_pool()
    _reset_autowarmup_table_pool()
    _reset_httpx_pool()
    reset_openapi_rate_limit_state()
