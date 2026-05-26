"""Tests for State Repo behavior.

Responsibility: Tests for State Repo behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_job_state_round_trips_parent_job_id`,
`test_job_state_writes_canonical_v2_job_metadata`, `test_job_state_honours_explicit_job_title`,
`test_list_for_owner_ensures_missing_jobstate_table`,
`test_list_children_for_owner_groups_parent_rows`,
`test_create_ensures_state_and_history_tables`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_state_repo.py`.
"""

from __future__ import annotations

from api.services.state import repository as state_repo
from api.services.state.repository import JobState, JobStateRepository
from azure.core.exceptions import ResourceNotFoundError


def test_job_state_round_trips_parent_job_id() -> None:
    state = JobState(
        job_id="child-1",
        type="blast-child",
        status="queued",
        parent_job_id="parent-1",
        payload={"group_id": "qg1"},
    )

    entity = state.to_entity()
    restored = JobState.from_entity(entity)

    assert entity["parent_job_id"] == "parent-1"
    assert restored.parent_job_id == "parent-1"
    assert restored.payload == {"group_id": "qg1"}


def test_job_state_writes_canonical_v2_job_metadata() -> None:
    state = JobState(
        job_id="job-1",
        type="blast",
        status="queued",
        payload={
            "program": "blastn",
            "db": "https://acct.blob.core.windows.net/blast-db/core_nt/core_nt",
            "query_file": "queries/uploads/job-1/query.fa",
            "subscription_id": "sub-1",
            "resource_group": "rg-elb-01",
            "cluster_name": "elb-aks",
            "storage_account": "elbstg01",
        },
    )

    entity = state.to_entity()
    restored = JobState.from_entity(entity)

    assert entity["schema_version"] == 2
    assert entity["job_title"] == "blastn - core_nt - query.fa"
    assert entity["program"] == "blastn"
    assert entity["db"] == "core_nt"
    assert entity["query_label"] == "query.fa"
    assert entity["subscription_id"] == "sub-1"
    assert entity["resource_group"] == "rg-elb-01"
    assert entity["cluster_name"] == "elb-aks"
    assert entity["storage_account"] == "elbstg01"
    assert restored.job_title == "blastn - core_nt - query.fa"
    assert restored.query_label == "query.fa"


def test_job_state_honours_explicit_job_title() -> None:
    state = JobState(
        job_id="job-2",
        type="blast",
        status="queued",
        payload={"job_title": "Sample panel search", "program": "blastn", "db": "core_nt"},
    )

    entity = state.to_entity()
    restored = JobState.from_entity(entity)

    assert entity["job_title"] == "Sample panel search"
    assert restored.job_title == "Sample panel search"


def test_list_for_owner_ensures_missing_jobstate_table(monkeypatch) -> None:
    created_tables: list[str] = []

    class MissingTableClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> MissingTableClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def query_entities(self, *_args: object, **_kwargs: object) -> list[dict[str, str]]:
            raise ResourceNotFoundError(message="table missing")

    class RecordingTableService:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> RecordingTableService:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def create_table_if_not_exists(self, table_name: str) -> None:
            created_tables.append(table_name)

    monkeypatch.setenv("AZURE_TABLE_ENDPOINT", "https://acct.table.core.windows.net")
    monkeypatch.setattr(state_repo, "TableClient", MissingTableClient)
    monkeypatch.setattr(state_repo, "TableServiceClient", RecordingTableService)
    monkeypatch.setattr(state_repo, "get_credential", lambda: object())
    state_repo._ENSURED_TABLES.clear()

    repo = JobStateRepository()

    assert repo.list_for_owner("owner-1") == []
    assert created_tables == ["jobstate"]


def test_list_for_scope_is_owner_agnostic_but_requires_scope(monkeypatch) -> None:
    queries: list[str] = []
    rows = [
        JobState(
            job_id="job-other-owner",
            type="blast",
            status="failed",
            owner_oid="owner-from-previous-login",
            subscription_id="sub-1",
            resource_group="rg-elb-cluster",
            cluster_name="elb-cluster-01",
            created_at="2026-05-26T06:21:28Z",
        ).to_entity(),
    ]

    class RecordingTableClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> RecordingTableClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def query_entities(self, query_filter: str, **_kwargs: object) -> list[dict[str, object]]:
            queries.append(query_filter)
            return rows

    monkeypatch.setenv("AZURE_TABLE_ENDPOINT", "https://acct.table.core.windows.net")
    monkeypatch.setattr(state_repo, "TableClient", RecordingTableClient)
    monkeypatch.setattr(state_repo, "get_credential", lambda: object())

    repo = JobStateRepository()

    assert repo.list_for_scope() == []
    scoped = repo.list_for_scope(
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
        cluster_name="elb-cluster-01",
        include_payload=False,
    )

    assert [row.job_id for row in scoped] == ["job-other-owner"]
    assert queries == [
        "status ne 'deleted' and subscription_id eq 'sub-1' "
        "and resource_group eq 'rg-elb-cluster' and cluster_name eq 'elb-cluster-01'"
    ]


def test_list_children_for_owner_groups_parent_rows(monkeypatch) -> None:
    queries: list[tuple[str, int]] = []
    rows = [
        JobState(
            job_id="child-2",
            type="blast-child",
            status="running",
            owner_oid="owner-1",
            parent_job_id="parent-1",
            created_at="2026-05-17T00:02:00Z",
        ).to_entity(),
        JobState(
            job_id="child-1",
            type="blast-child",
            status="completed",
            owner_oid="owner-1",
            parent_job_id="parent-1",
            created_at="2026-05-17T00:01:00Z",
        ).to_entity(),
        JobState(
            job_id="child-other",
            type="blast-child",
            status="completed",
            owner_oid="owner-1",
            parent_job_id="other-parent",
            created_at="2026-05-17T00:00:00Z",
        ).to_entity(),
    ]

    class RecordingTableClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> RecordingTableClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def query_entities(self, query_filter: str, *, results_per_page: int):
            queries.append((query_filter, results_per_page))
            return rows

    monkeypatch.setenv("AZURE_TABLE_ENDPOINT", "https://acct.table.core.windows.net")
    monkeypatch.setattr(state_repo, "TableClient", RecordingTableClient)
    monkeypatch.setattr(state_repo, "get_credential", lambda: object())

    repo = JobStateRepository()
    grouped = repo.list_children_for_owner(
        "owner-1",
        ["parent-1", "parent-2"],
        limit=10,
    )

    assert queries == [("owner_oid eq 'owner-1' and parent_job_id ne ''", 10)]
    assert set(grouped) == {"parent-1", "parent-2"}
    assert [row.job_id for row in grouped["parent-1"]] == ["child-1", "child-2"]
    assert grouped["parent-2"] == []


def test_create_ensures_state_and_history_tables(monkeypatch) -> None:
    created_tables: list[str] = []
    entities: list[dict[str, object]] = []

    class RecordingTableClient:
        def __init__(self, **kwargs: object) -> None:
            self.table_name = str(kwargs["table_name"])

        def __enter__(self) -> RecordingTableClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def create_entity(self, entity: dict[str, object]) -> None:
            entities.append({"table_name": self.table_name, **entity})

    class RecordingTableService:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> RecordingTableService:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def create_table_if_not_exists(self, table_name: str) -> None:
            created_tables.append(table_name)

    monkeypatch.setenv("AZURE_TABLE_ENDPOINT", "https://acct.table.core.windows.net")
    monkeypatch.setattr(state_repo, "TableClient", RecordingTableClient)
    monkeypatch.setattr(state_repo, "TableServiceClient", RecordingTableService)
    monkeypatch.setattr(state_repo, "get_credential", lambda: object())
    state_repo._ENSURED_TABLES.clear()

    repo = JobStateRepository()
    repo.create(JobState(job_id="job-1", type="blast", status="queued"))

    assert created_tables == ["jobstate", "jobhistory"]
    assert [entity["table_name"] for entity in entities] == ["jobstate", "jobhistory"]


def test_list_for_owner_includes_cluster_shared_rows(monkeypatch) -> None:
    """``list_for_owner`` MUST include rows with owner_oid=='' (external sync)."""
    captured_filters: list[str] = []

    class RecordingTableClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> RecordingTableClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def query_entities(self, query_filter: str, *, results_per_page: int):
            captured_filters.append(query_filter)
            return []

    monkeypatch.setenv("AZURE_TABLE_ENDPOINT", "https://acct.table.core.windows.net")
    monkeypatch.setattr(state_repo, "TableClient", RecordingTableClient)
    monkeypatch.setattr(state_repo, "get_credential", lambda: object())

    repo = JobStateRepository()
    repo.list_for_owner("owner-1")

    assert captured_filters == [
        "(owner_oid eq 'owner-1' or owner_oid eq '') and status ne 'deleted'"
    ]


def test_get_many_batches_into_single_query(monkeypatch) -> None:
    """get_many MUST issue a single OData query covering all ids."""
    captured: list[str] = []
    rows = [
        JobState(
            job_id="abc",
            type="blast",
            status="completed",
        ).to_entity(),
        JobState(
            job_id="def",
            type="blast",
            status="failed",
        ).to_entity(),
    ]

    class RecordingTableClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> RecordingTableClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def query_entities(self, query_filter: str, *, results_per_page: int):
            captured.append(query_filter)
            return rows

    monkeypatch.setenv("AZURE_TABLE_ENDPOINT", "https://acct.table.core.windows.net")
    monkeypatch.setattr(state_repo, "TableClient", RecordingTableClient)
    monkeypatch.setattr(state_repo, "get_credential", lambda: object())

    repo = JobStateRepository()
    out = repo.get_many(["abc", "def", "abc"])  # duplicate should be de-duped

    assert len(captured) == 1
    assert "(PartitionKey eq 'abc' and RowKey eq 'current')" in captured[0]
    assert "(PartitionKey eq 'def' and RowKey eq 'current')" in captured[0]
    assert set(out) == {"abc", "def"}


def test_create_returns_existing_on_resource_exists(monkeypatch) -> None:
    """Concurrent create races MUST return the existing row, not raise."""
    from azure.core.exceptions import ResourceExistsError

    existing_entity = JobState(
        job_id="raced",
        type="blast",
        status="running",
    ).to_entity()

    class RacingTableClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> RacingTableClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def create_entity(self, _entity: dict[str, object]) -> None:
            raise ResourceExistsError(message="duplicate")

        def get_entity(self, *, partition_key: str, row_key: str) -> dict[str, object]:
            assert partition_key == "raced"
            assert row_key == "current"
            return existing_entity

    class NoopTableService:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> NoopTableService:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def create_table_if_not_exists(self, _table_name: str) -> None:
            pass

    monkeypatch.setenv("AZURE_TABLE_ENDPOINT", "https://acct.table.core.windows.net")
    monkeypatch.setattr(state_repo, "TableClient", RacingTableClient)
    monkeypatch.setattr(state_repo, "TableServiceClient", NoopTableService)
    monkeypatch.setattr(state_repo, "get_credential", lambda: object())
    state_repo._ENSURED_TABLES.clear()

    repo = JobStateRepository()
    returned = repo.create(JobState(job_id="raced", type="blast", status="queued"))

    # Existing row wins; the caller's "queued" status is not persisted.
    assert returned.job_id == "raced"
    assert returned.status == "running"


def test_list_active_filters_to_in_flight_states(monkeypatch) -> None:
    """list_active MUST scope to in-flight statuses and the requested type."""
    captured: list[str] = []

    class RecordingTableClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> RecordingTableClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def query_entities(self, query_filter: str, *, results_per_page: int):
            captured.append(query_filter)
            return []

    monkeypatch.setenv("AZURE_TABLE_ENDPOINT", "https://acct.table.core.windows.net")
    monkeypatch.setattr(state_repo, "TableClient", RecordingTableClient)
    monkeypatch.setattr(state_repo, "get_credential", lambda: object())

    repo = JobStateRepository()
    repo.list_active(job_type="blast")

    assert captured, "list_active must issue a Table query"
    filter_expr = captured[0]
    assert "type eq 'blast'" in filter_expr
    for active in ("queued", "pending", "running", "reducing"):
        assert f"status eq '{active}'" in filter_expr


def test_list_completed_filters_to_completed_state(monkeypatch) -> None:
    captured: list[str] = []

    class RecordingTableClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> RecordingTableClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def query_entities(self, query_filter: str, *, results_per_page: int):
            captured.append(query_filter)
            return []

    monkeypatch.setenv("AZURE_TABLE_ENDPOINT", "https://acct.table.core.windows.net")
    monkeypatch.setattr(state_repo, "TableClient", RecordingTableClient)
    monkeypatch.setattr(state_repo, "get_credential", lambda: object())

    repo = JobStateRepository()
    repo.list_completed(job_type="blast")

    assert captured == ["type eq 'blast' and status eq 'completed'"]


def test_list_methods_clamp_page_size_to_azure_tables_max(monkeypatch) -> None:
    """Regression: Azure Tables rejects ``results_per_page > 1000`` with
    ``InvalidInput``. The cancel task previously passed ``limit=10_000``
    straight through to ``list_children`` which made the whole cancel
    pipeline fail (HTTP 400 InvalidInput → ``cancel_unavailable`` → the
    cluster card kept showing "Running" because the K8s Jobs were never
    deleted). Pin the clamp so the bug cannot regress for any list_* path
    that takes a caller-supplied ``limit``.
    """
    captured: list[tuple[str, int]] = []

    class RecordingTableClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> RecordingTableClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def query_entities(self, query_filter: str, *, results_per_page: int, **_kw):
            captured.append((query_filter, results_per_page))
            return []

    monkeypatch.setenv("AZURE_TABLE_ENDPOINT", "https://acct.table.core.windows.net")
    monkeypatch.setattr(state_repo, "TableClient", RecordingTableClient)
    monkeypatch.setattr(state_repo, "get_credential", lambda: object())

    repo = JobStateRepository()

    # The exact value the cancel task asks for.
    repo.list_children("parent-1", limit=10_000)
    # Other list_* paths whose limit is caller-controlled.
    repo.list_active(job_type="blast", limit=10_000)
    repo.list_completed(job_type="blast", limit=10_000)
    repo.list_children_for_owner("owner-1", ["parent-1"], limit=10_000)
    repo.get_history("job-1", limit=10_000)

    # All page sizes must stay <= 1000 (Azure Tables hard max).
    page_sizes = [page for _filter, page in captured]
    assert page_sizes, "no query_entities calls captured"
    assert all(p <= 1000 for p in page_sizes), (
        f"page sizes exceed Azure Tables max: {page_sizes!r}"
    )
