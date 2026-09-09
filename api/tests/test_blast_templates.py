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

    def query_entities(
        self, query: str, results_per_page: int | None = None
    ) -> list[dict[str, Any]]:
        m = re.search(r"PartitionKey eq '([^']*)'", query)
        pk = m.group(1) if m else None
        rows = [dict(v) for k, v in self.store.items() if k[0] == pk]
        return rows[:results_per_page] if results_per_page is not None else rows

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
    t = tmpl.create_template("oid-1", "old", {"evalue": 1})
    updated = tmpl.update_template("oid-1", t.id, name="new", fields={"evalue": 2})
    assert updated is not None
    assert updated.name == "new"
    assert updated.fields == {"evalue": 2}
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


def test_rename_to_duplicate_name_rejected(store: dict) -> None:
    first = tmpl.create_template("oid-1", "first", {})
    tmpl.create_template("oid-1", "second", {})

    with pytest.raises(tmpl.TemplateValidationError):
        tmpl.update_template("oid-1", first.id, name="second")


def test_name_control_chars_stripped(store: dict) -> None:
    t = tmpl.create_template("oid-1", "ab\x00c\x1fd", {})
    assert t.name == "abcd"


def test_fields_key_count_cap(store: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmpl, "_MAX_FIELDS_KEYS", 3)
    with pytest.raises(tmpl.TemplateValidationError):
        tmpl.create_template("oid-1", "a", {"a": 1, "b": 2, "c": 3, "d": 4})


@pytest.mark.parametrize(
    "field",
    [
        "query_data",
        "query_accession",
        "query_blob_url",
        "query_file",
        "query_from",
        "query_to",
        "job_title",
        "idempotency_key",
        "external_correlation_id",
    ],
)
def test_query_and_execution_identity_fields_are_rejected(store: dict, field: str) -> None:
    with pytest.raises(tmpl.TemplateValidationError):
        tmpl.create_template("oid-1", "unsafe", {field: "private"})


def test_unknown_nested_and_nonfinite_template_fields_are_rejected(store: dict) -> None:
    with pytest.raises(tmpl.TemplateValidationError):
        tmpl.create_template("oid-1", "unknown", {"future_query_alias": ">private"})
    with pytest.raises(tmpl.TemplateValidationError):
        tmpl.create_template("oid-1", "nested", {"program": {"query_data": ">private"}})
    with pytest.raises(tmpl.TemplateValidationError):
        tmpl.create_template("oid-1", "nonfinite", {"evalue": float("nan")})
    with pytest.raises(tmpl.TemplateValidationError):
        tmpl.create_template("oid-1", "wrong-type", {"program": 123})
    with pytest.raises(tmpl.TemplateValidationError):
        tmpl.create_template("oid-1", "wrong-enum", {"sharding_mode": "invalid"})
    for options in (
        "-query private.fa",
        "-query_loc 10-20",
        "-subject=private.fa",
        "-subject_loc=30-40",
    ):
        with pytest.raises(tmpl.TemplateValidationError):
            tmpl.create_template("oid-1", "per-run-option", {"additional_options": options})
    for fields in (
        {"evalue": -1},
        {"max_target_seqs": 0},
        {"outfmt": 999},
        {"word_size": "not-a-number"},
        {"taxid": "abc"},
        {"additional_options": "x" * 1025},
    ):
        with pytest.raises(tmpl.TemplateValidationError):
            tmpl.create_template("oid-1", "invalid-value", fields)


def test_legacy_unsafe_fields_are_filtered_on_read(store: dict) -> None:
    partition = tmpl._partition_key("oid-1")
    store[(partition, "legacy")] = {
        "PartitionKey": partition,
        "RowKey": "legacy",
        "owner_oid": "oid-1",
        "name": "legacy\x00" + "x" * 200,
        "fields_json": (
            '{"program":"blastn","query_data":">private",'
            '"sharding_mode":"invalid","max_target_seqs":"100",'
            '"additional_options":"-query_loc 10-20","extra":{"x":1}}'
        ),
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }

    template = tmpl.list_templates("oid-1")[0]
    assert template.fields == {"program": "blastn"}
    assert "\x00" not in template.name
    assert len(template.name) == 120


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


def test_route_storage_failure_is_503(
    client: TestClient, store: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    del store
    monkeypatch.setattr(tmpl, "_ensure_table", lambda: (_ for _ in ()).throw(OSError("down")))

    response = client.get("/api/blast/templates")

    assert response.status_code == 503
    assert response.json()["detail"] == "template storage is unavailable"
