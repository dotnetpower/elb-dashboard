"""Tests for Route Contracts behavior.

Responsibility: Tests for Route Contracts behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_route_index`, `test_blast_package_keeps_public_import_surface`,
`test_aks_package_keeps_public_import_surface`,
`test_storage_package_keeps_public_import_surface`,
`test_blast_specific_result_routes_precede_file_id_catchall`,
`test_api_routes_registered_before_frontend_catchall`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

from api.main import app
from api.routes import aks, blast, storage
from fastapi.routing import APIRoute


def _route_index() -> dict[tuple[str, str], int]:
    index: dict[tuple[str, str], int] = {}
    for position, route in enumerate(app.routes):
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or set():
            index[(method, route.path)] = position
    return index


def test_blast_package_keeps_public_import_surface() -> None:
    assert blast.blast_router.prefix == "/api/blast"
    assert callable(blast.blast_submit)
    assert callable(blast._config_preview_from_payload)
    assert callable(blast._openapi_client_kwargs_from_cluster)
    assert callable(blast._safe_delay)


def test_aks_package_keeps_public_import_surface() -> None:
    assert aks.aks_router.prefix == "/api/aks"
    assert callable(aks.aks_skus)
    assert callable(aks.aks_provision)
    assert callable(aks.aks_openapi_deploy)
    assert callable(aks.aks_openapi_deployment)
    assert callable(aks.aks_openapi_proxy)
    assert callable(aks.aks_openapi_token)
    assert callable(aks.aks_openapi_token_generate)
    assert callable(aks.aks_start)
    assert callable(aks.aks_assign_roles)
    assert callable(aks._invalidate_aks_monitor_cache)


def test_storage_package_keeps_public_import_surface() -> None:
    assert storage.router.prefix == "/api/storage"
    assert callable(storage.prepare_db)
    assert callable(storage.storage_local_debug_status)
    assert callable(storage.storage_local_debug_open)


def test_blast_specific_result_routes_precede_file_id_catchall() -> None:
    routes = _route_index()
    catchall = routes[("GET", "/api/blast/jobs/{job_id}/results/{file_id}")]
    for path in (
        "/api/blast/jobs/{job_id}/results/aggregate",
        "/api/blast/jobs/{job_id}/results/alignments",
        "/api/blast/jobs/{job_id}/results/taxonomy",
        "/api/blast/jobs/{job_id}/results/download",
        "/api/blast/jobs/{job_id}/results/export",
    ):
        assert routes[("GET", path)] < catchall


def test_api_routes_registered_before_frontend_catchall() -> None:
    routes = _route_index()
    frontend = routes[("GET", "/{full_path:path}")]
    for method, path in (
        ("GET", "/api/blast/jobs"),
        ("POST", "/api/blast/jobs"),
        ("GET", "/api/aks/skus"),
        ("POST", "/api/aks/provision"),
        ("GET", "/api/aks/openapi/deployment"),
        ("GET", "/api/aks/openapi/spec"),
        ("GET", "/api/aks/openapi/token"),
        ("POST", "/api/aks/openapi/token"),
        ("GET", "/api/aks/openapi/proxy"),
        ("POST", "/api/storage/prepare-db"),
        ("GET", "/api/storage/local-debug"),
        ("POST", "/api/storage/local-debug/open"),
        ("GET", "/api/monitor/aks"),
        ("GET", "/api/monitor/sidecars"),
    ):
        assert routes[(method, path)] < frontend
