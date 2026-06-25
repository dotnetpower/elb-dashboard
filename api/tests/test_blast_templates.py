"""Tests for per-user BLAST submit templates (service CRUD + routes).

Responsibility: Cover ``api.services.blast.submit_templates`` CRUD with an
in-memory fake table (create/list/update/delete, count + size + name limits) and
the ``/api/blast/templates`` route contracts.
Edit boundaries: Test-only; monkeypatches the table client so no Azure is touched.
Key entry points: pytest test functions.
Risky contracts: per-user partition isolation, count/size/name caps, 404 on
missing, 400 on validation error.
Validation: ``uv run pytest -q api/tests/test_blast_templates.py``.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from api.services.blast import submit_templates as tmpl
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from fastapi.testclient import TestClient


class FakeTable:
    def __init__(self, store: dict[tuple[str, str], dict[str, Any]]) -> None:
        self.store = store

    def __enter__(self) -> FakeTable:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def create_entity(self, entity: dict[str, Any]) -> None:
        key = (entity["PartitionKey"], entity["RowKey"])
        if key in self.store:
            raise ResourceExistsError("exists")
        self.store[key] = dict(entity)

    def get_entity(self, partition_key: str, row_key: str) -> dict[str, Any]:
        key = (partition_key, row_key)
        if key not in self.store:
            raise ResourceNotFoundError("missing")
        return dict(self.store[key])

    def query_entities(self, query: str) -> list[dict[str, Any]]:
        m = re.search(r"PartitionKey eq '([^']*)'", query)
        pk = m.group(1) if m else None
        return [dict(v) for k, v in self.store.items() if k[0] == pk]

    def upsert_entity(self, entity: dict[str, Any], mode: Any = None) -> None:
        del mode
        self.store[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)

    def delete_entity(self, partition_key: str, row_key: str) -> None:
        key = (partition_key, row_key)
        if key not in self.store:
            raise ResourceNotFoundError("missing")
        del self.store[key]


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], dict[str, Any]]:
    data: dict[tuple[str, str], dict[str, Any]] = {}
    monkeypatch.setattr(tmpl, "_ensure_table", lambda: None)
    monkeypatch.setattr(tmpl, "_table_client", lambda: FakeTable(data))
    return data


def test_create_and_list(store: dict) -> None:
    t = tmpl.create_template("oid-1", "My nt scan", {"program": "blastn", "db": "nt"})
    assert t.id
    assert t.name == "My nt scan"
    listed = tmpl.list_templates("oid-1")
    assert [x.id for x in listed] == [t.id]
    assert listed[0].fields == {"program": "blastn", "db": "nt"}


def test_partition_isolation(store: dict) -> None:
    tmpl.create_template("oid-1", "a", {})
    tmpl.create_template("oid-2", "b", {})
    assert len(tmpl.list_templates("oid-1")) == 1
    assert len(tmpl.list_templates("oid-2")) == 1


def test_update(store: dict) -> None:
    t = tmpl.create_template("oid-1", "old", {"x": 1})
    updated = tmpl.update_template("oid-1", t.id, name="new", fields={"x": 2})
    assert updated is not None
    assert updated.name == "new"
    assert updated.fields == {"x": 2}
    assert updated.created_at == t.created_at


def test_update_missing_returns_none(store: dict) -> None:
    assert tmpl.update_template("oid-1", "nope", name="x") is None


def test_delete(store: dict) -> None:
    t = tmpl.create_template("oid-1", "a", {})
    assert tmpl.delete_template("oid-1", t.id) is True
    assert tmpl.delete_template("oid-1", t.id) is False
    assert tmpl.list_templates("oid-1") == []


def test_count_cap(store: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmpl, "_MAX_TEMPLATES_PER_USER", 2)
    tmpl.create_template("oid-1", "a", {})
    tmpl.create_template("oid-1", "b", {})
    with pytest.raises(tmpl.TemplateValidationError):
        tmpl.create_template("oid-1", "c", {})


def test_validate_name_required(store: dict) -> None:
    with pytest.raises(tmpl.TemplateValidationError):
        tmpl.create_template("oid-1", "   ", {})


def test_validate_fields_must_be_object(store: dict) -> None:
    with pytest.raises(tmpl.TemplateValidationError):
        tmpl.create_template("oid-1", "a", ["not", "a", "dict"])


def test_validate_fields_size_cap(store: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmpl, "_MAX_FIELDS_BYTES", 50)
    with pytest.raises(tmpl.TemplateValidationError):
        tmpl.create_template("oid-1", "a", {"big": "x" * 200})


def test_duplicate_name_rejected(store: dict) -> None:
    tmpl.create_template("oid-1", "dup", {})
    with pytest.raises(tmpl.TemplateValidationError):
        tmpl.create_template("oid-1", "dup", {})


def test_name_control_chars_stripped(store: dict) -> None:
    t = tmpl.create_template("oid-1", "ab\x00c\x1fd", {})
    assert t.name == "abcd"


def test_fields_key_count_cap(store: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmpl, "_MAX_FIELDS_KEYS", 3)
    with pytest.raises(tmpl.TemplateValidationError):
        tmpl.create_template("oid-1", "a", {"a": 1, "b": 2, "c": 3, "d": 4})


# ---- route contract tests ---------------------------------------------------


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    from api.main import app

    return TestClient(app)


def test_routes_crud(client: TestClient, store: dict) -> None:
    # create
    r = client.post("/api/blast/templates", json={"name": "scan", "fields": {"db": "nt"}})
    assert r.status_code == 201
    tid = r.json()["id"]
    # list
    r = client.get("/api/blast/templates")
    assert r.status_code == 200
    assert any(t["id"] == tid for t in r.json()["templates"])
    # update
    r = client.put(f"/api/blast/templates/{tid}", json={"name": "scan2"})
    assert r.status_code == 200
    assert r.json()["name"] == "scan2"
    # delete
    r = client.delete(f"/api/blast/templates/{tid}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True


def test_route_update_missing_404(client: TestClient, store: dict) -> None:
    r = client.put("/api/blast/templates/nope", json={"name": "x"})
    assert r.status_code == 404


def test_route_delete_missing_404(client: TestClient, store: dict) -> None:
    r = client.delete("/api/blast/templates/nope")
    assert r.status_code == 404


def test_route_bad_id_422(client: TestClient, store: dict) -> None:
    r = client.delete("/api/blast/templates/bad!id")
    assert r.status_code == 422


def test_route_create_validation_400(
    client: TestClient, store: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tmpl, "_MAX_FIELDS_BYTES", 10)
    r = client.post(
        "/api/blast/templates", json={"name": "a", "fields": {"big": "x" * 100}}
    )
    assert r.status_code == 400
