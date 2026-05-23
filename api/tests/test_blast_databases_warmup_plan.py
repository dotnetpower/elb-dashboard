"""Integration tests for /api/blast/databases warmup_plan enrichment.

Responsibility: Integration tests for /api/blast/databases warmup_plan enrichment
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `client`, `fake_list_databases`, `test_no_cluster_params_omits_warmup_plan`,
`test_partial_cluster_params_omits_warmup_plan`,
`test_warmup_plan_attached_when_cluster_supplied`,
`test_negative_num_nodes_rejected_by_query_validation`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_blast_databases_warmup_plan.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    return TestClient(app)


_FAKE_DBS: list[dict[str, Any]] = [
    {
        "name": "16S_ribosomal_RNA",
        "total_bytes": 20 * 1024 * 1024,
        "files": 12,
    },
    {
        "name": "core_nt",
        "total_bytes": int(283.62 * 1024**3),
        "files": 200,
    },
    {
        "name": "nr_huge",
        "total_bytes": 1500 * 1024**3,
        "files": 400,
    },
]


@pytest.fixture()
def fake_list_databases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass Azure SDK by replacing list_databases with a fixture."""

    def _fake(_cred: Any, _account: str) -> list[dict[str, Any]]:
        # Return deep copies so the route's enrichment cannot leak across calls.
        import copy

        return copy.deepcopy(_FAKE_DBS)

    monkeypatch.setattr("api.services.storage.data.list_databases", _fake, raising=True)

    # Also short-circuit the local-debug auto-open helper so the test does
    # not try to call ARM. Returning {} == "no action taken".
    def _no_access(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"action": "noop"}

    monkeypatch.setattr(
        "api.services.storage.public_access.ensure_local_storage_access",
        _no_access,
        raising=True,
    )


# ---------------------------------------------------------------------------
# Backward compat — no cluster info -> no warmup_plan field
# ---------------------------------------------------------------------------
def test_no_cluster_params_omits_warmup_plan(client: TestClient, fake_list_databases: None) -> None:
    r = client.get(
        "/api/blast/databases",
        params={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "storage_account": "stfake",
            "resource_group": "rg-fake",
        },
    )
    assert r.status_code == 200
    for db in r.json()["databases"]:
        assert "warmup_plan" not in db, (
            "warmup_plan must be omitted when cluster topology is not supplied"
        )


def test_partial_cluster_params_omits_warmup_plan(
    client: TestClient, fake_list_databases: None
) -> None:
    """Either both or neither — never half-attached."""
    r = client.get(
        "/api/blast/databases",
        params={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "storage_account": "stfake",
            "resource_group": "rg-fake",
            "num_nodes": 3,
            # machine_type intentionally missing
        },
    )
    assert r.status_code == 200
    for db in r.json()["databases"]:
        assert "warmup_plan" not in db


# ---------------------------------------------------------------------------
# Happy path enrichment
# ---------------------------------------------------------------------------
def test_warmup_plan_attached_when_cluster_supplied(
    client: TestClient, fake_list_databases: None
) -> None:
    r = client.get(
        "/api/blast/databases",
        params={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "storage_account": "stfake",
            "resource_group": "rg-fake",
            "num_nodes": 3,
            "machine_type": "Standard_E32s_v5",
        },
    )
    assert r.status_code == 200
    by_name = {db["name"]: db for db in r.json()["databases"]}

    # 16S — trivially feasible
    plan_16s = by_name["16S_ribosomal_RNA"]["warmup_plan"]
    assert plan_16s["feasible"] is True
    assert plan_16s["status"] == "ok"
    assert plan_16s["chosen_shards"] == 3

    # core_nt — feasible on 3 × E32s_v5 (per_node ≈ 94 GiB ≤ 128 GiB)
    plan_core = by_name["core_nt"]["warmup_plan"]
    assert plan_core["feasible"] is True
    assert plan_core["status"] == "ok"
    assert 94.0 <= plan_core["per_shard_gib"] <= 95.0

    # nr_huge 1.5 TiB — node SKU too small
    plan_nr = by_name["nr_huge"]["warmup_plan"]
    assert plan_nr["feasible"] is False
    assert plan_nr["status"] == "node_sku_too_small"
    assert plan_nr["recommendations"], "expected recommendations to be non-empty"


# ---------------------------------------------------------------------------
# Validation — node count guard
# ---------------------------------------------------------------------------
def test_negative_num_nodes_rejected_by_query_validation(
    client: TestClient, fake_list_databases: None
) -> None:
    r = client.get(
        "/api/blast/databases",
        params={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "storage_account": "stfake",
            "resource_group": "rg-fake",
            "num_nodes": -1,
            "machine_type": "Standard_E32s_v5",
        },
    )
    assert r.status_code == 422  # FastAPI ge=0 rejection


def test_zero_num_nodes_treated_as_unspecified(
    client: TestClient, fake_list_databases: None
) -> None:
    r = client.get(
        "/api/blast/databases",
        params={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "storage_account": "stfake",
            "resource_group": "rg-fake",
            "num_nodes": 0,
            "machine_type": "Standard_E32s_v5",
        },
    )
    assert r.status_code == 200
    for db in r.json()["databases"]:
        assert "warmup_plan" not in db
