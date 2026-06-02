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
    # The AKS runtime-RBAC helper defaults the workload-Storage target to
    # the platform env when the caller omits it (api/tasks/azure/rbac.py
    # `_resolve_workload_storage_defaults`). Tests that exercise the
    # "no storage target" path must not pick up ambient azd env values.
    monkeypatch.delenv("AZURE_STORAGE_ACCOUNT", raising=False)
    monkeypatch.delenv("STORAGE_ACCOUNT_NAME", raising=False)
    monkeypatch.delenv("AZURE_RESOURCE_GROUP", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path_factory.mktemp("elb_state")))


@pytest.fixture(autouse=True)
def _stub_blast_submit_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-pass for the BLAST submit fail-closed gates.

    The synchronous gate evaluator hits the terminal sidecar / broker / ARM /
    Storage; almost no existing test stubs those, so without this fixture every
    ``POST /api/blast/submit`` test that used to succeed would now 409, and the
    pre-flight route (which surfaces the local sidecar gates inline) would
    sprout spurious ``fail`` rows. Tests that exercise the gates themselves
    patch ``evaluate_submit_gates`` and the individual ``_gate_*`` helpers
    again inside the test body to override the default-pass.
    """
    from api.services.blast import submit_gates

    def _allow_all(**_kwargs: object) -> submit_gates.SubmitGatesReport:
        return submit_gates.SubmitGatesReport(ok=True, gates=[], blocking=[])

    def _ok(gate_id: str) -> submit_gates.GateResult:
        return submit_gates.GateResult(
            id=gate_id,
            status="ok",
            severity="critical",
            error_code="",
            message="default-pass under tests",
        )

    monkeypatch.setattr(submit_gates, "evaluate_submit_gates", _allow_all)
    monkeypatch.setattr(submit_gates, "_gate_exec_token", lambda: _ok("exec_token"))
    monkeypatch.setattr(
        submit_gates, "_gate_terminal_sidecar", lambda: _ok("terminal_sidecar")
    )
    submit_gates.reset_submit_gates_cache()


@pytest.fixture(autouse=True)
def _reset_external_jobs_cache() -> Generator[None, None, None]:
    """Clear the in-memory external-OpenAPI jobs cache between every test.

    Without this, a test that mocks ``external_blast.list_jobs`` with one
    response can leak that response into a subsequent test whose mock
    expects to be the only source of truth.
    """
    from api.routes._blast_shared import _reset_external_jobs_cache as _reset
    from api.routes.aks.autostop import _reset_status_cache as _reset_autostop_status_cache
    from api.routes.blast.jobs import _reset_blast_jobs_list_cache
    from api.routes.storage.common import reset_ncbi_catalogue_cache
    from api.services.auto_stop import _reset_autostop_table_pool
    from api.services.auto_warmup import _reset_autowarmup_table_pool
    from api.services.azure_clients import reset_mgmt_client_pool
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
    reset_mgmt_client_pool()
    reset_ncbi_catalogue_cache()
    reset_k8s_credential_cache()
    reset_k8s_session_pool()
    reset_redis_clients()
    _reset_artifact_table_pool()
    _reset_autowarmup_table_pool()
    _reset_autostop_table_pool()
    _reset_autostop_status_cache()
    _reset_httpx_pool()
    reset_openapi_rate_limit_state()
    yield
    _reset()
    _reset_blast_jobs_list_cache()
    _reset_blast_db_metadata_cache()
    _reset_blast_status_cache()
    reset_state_repo_cache()
    reset_blob_service_pool()
    reset_mgmt_client_pool()
    reset_ncbi_catalogue_cache()
    reset_k8s_session_pool()
    reset_k8s_credential_cache()
    reset_redis_clients()
    _reset_artifact_table_pool()
    _reset_autowarmup_table_pool()
    _reset_autostop_table_pool()
    _reset_autostop_status_cache()
    _reset_httpx_pool()
    reset_openapi_rate_limit_state()
