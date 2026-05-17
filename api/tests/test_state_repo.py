from __future__ import annotations

from api.services import state_repo
from api.services.state_repo import JobState, JobStateRepository
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
