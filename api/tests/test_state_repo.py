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


def test_job_state_round_trips_owner_upn() -> None:
    # The User column on Recent searches reads owner_upn — guard the round
    # trip so a future schema edit can't silently drop it.
    state = JobState(
        job_id="job-upn",
        type="blast",
        status="queued",
        owner_oid="oid-1",
        owner_upn="alice@example.com",
    )

    entity = state.to_entity()
    restored = JobState.from_entity(entity)

    assert entity["owner_upn"] == "alice@example.com"
    assert restored.owner_upn == "alice@example.com"

    # Missing owner_upn round trips to None (legacy rows).
    legacy_entity = dict(entity)
    legacy_entity["owner_upn"] = ""
    legacy_restored = JobState.from_entity(legacy_entity)
    assert legacy_restored.owner_upn is None


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
    # cluster_name is the strongest scope key — when supplied, resource_group
    # is intentionally omitted from the OData filter so jobs whose row was
    # saved with the cluster RG (e.g. rg-elb-cluster) still show up when the
    # caller is filtering from a dashboard whose workspace RG is different
    # (e.g. rg-elb-dashboard). See the docstring on list_for_scope.
    assert queries == [
        "status ne 'deleted' and subscription_id eq 'sub-1' "
        "and cluster_name eq 'elb-cluster-01'"
    ]


def test_list_for_scope_drops_rg_when_cluster_name_set(monkeypatch) -> None:
    """RG mismatch must not hide a row when caller supplied cluster_name.

    Reproduces the production bug where the Recent searches list silently
    rendered zero jobs because the SPA passed the dashboard workspace RG
    (``rg-elb-dashboard``) while the job row was saved with the cluster RG
    (``rg-elb-cluster``). The OData filter must drop the RG clause when
    ``cluster_name`` is provided so the row is returned.
    """
    queries: list[str] = []
    rows = [
        JobState(
            job_id="22cf0dae-a402-482e-9208-f07fe922957f",
            type="blast",
            status="running",
            owner_oid="owner-1",
            subscription_id="sub-1",
            resource_group="rg-elb-cluster",
            cluster_name="elb-cluster-01",
            created_at="2026-05-26T17:48:40Z",
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

    scoped = repo.list_for_scope(
        subscription_id="sub-1",
        resource_group="rg-elb-dashboard",  # workspace RG, NOT the cluster's RG
        cluster_name="elb-cluster-01",
        include_payload=False,
    )

    assert [row.job_id for row in scoped] == ["22cf0dae-a402-482e-9208-f07fe922957f"]
    assert queries == [
        "status ne 'deleted' and subscription_id eq 'sub-1' "
        "and cluster_name eq 'elb-cluster-01'"
    ]


def test_list_for_scope_uses_rg_when_cluster_name_omitted(monkeypatch) -> None:
    """RG is still a hard filter when no cluster_name is supplied."""
    queries: list[str] = []

    class RecordingTableClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> RecordingTableClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def query_entities(self, query_filter: str, **_kwargs: object) -> list[dict[str, object]]:
            queries.append(query_filter)
            return []

    monkeypatch.setenv("AZURE_TABLE_ENDPOINT", "https://acct.table.core.windows.net")
    monkeypatch.setattr(state_repo, "TableClient", RecordingTableClient)
    monkeypatch.setattr(state_repo, "get_credential", lambda: object())

    repo = JobStateRepository()

    repo.list_for_scope(
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
        include_payload=False,
    )

    assert queries == [
        "status ne 'deleted' and subscription_id eq 'sub-1' "
        "and resource_group eq 'rg-elb-cluster'"
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


def test_list_for_owner_returns_newest_beyond_first_page(monkeypatch) -> None:
    """``list_for_owner`` MUST return the genuinely most-recent ``limit`` rows.

    jobstate uses a random-uuid PartitionKey, so Azure returns rows in an
    order unrelated to ``created_at``. The previous implementation read only
    the first ``results_per_page=limit`` page and then sorted it, which
    silently dropped the newest jobs once an owner had more than ``limit`` of
    them. Here the newest rows are deliberately placed LAST in iteration
    order: a first-page read would miss them, so asserting they come back
    proves the full-scan-then-sort fix.
    """
    # Iteration order = oldest -> newest (newest last).
    rows = [
        JobState(
            job_id=f"job-{i}",
            type="blast",
            status="completed",
            owner_oid="owner-1",
            created_at=f"2026-06-0{i}T00:00:00Z",
        ).to_entity()
        for i in range(1, 6)
    ]

    class RecordingTableClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> RecordingTableClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def query_entities(self, _query_filter: str, **_kwargs: object):
            # Emulate the SDK paginating iterator: every matching row is
            # yielded regardless of results_per_page (page size only affects
            # round-trip batching, not the total set).
            return list(rows)

    monkeypatch.setenv("AZURE_TABLE_ENDPOINT", "https://acct.table.core.windows.net")
    monkeypatch.setattr(state_repo, "TableClient", RecordingTableClient)
    monkeypatch.setattr(state_repo, "get_credential", lambda: object())

    repo = JobStateRepository()
    result = repo.list_for_owner("owner-1", limit=3)

    assert [row.job_id for row in result] == ["job-5", "job-4", "job-3"]


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


def test_update_submits_only_the_changed_fields(monkeypatch) -> None:
    """``update`` MUST submit a MERGE patch, not the full read-back snapshot.

    Writing the whole snapshot back reverted any field a concurrent writer
    changed since our read (e.g. submit's ``task_id`` update clobbering the
    worker's fresh ``status="running"`` back to the stale ``"queued"``). The
    patch must carry PartitionKey/RowKey plus only the fields this call set.
    """
    submitted: list[dict[str, object]] = []
    existing_entity = JobState(
        job_id="job-merge",
        type="blast",
        status="queued",
        phase="queued",
        owner_oid="owner-1",
        payload={"job_title": "Panel search"},
    ).to_entity()

    class RecordingTableClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> RecordingTableClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def get_entity(self, *, partition_key: str, row_key: str) -> dict[str, object]:
            assert partition_key == "job-merge"
            assert row_key == "current"
            return dict(existing_entity)

        def update_entity(self, entity: dict[str, object], **_kwargs: object) -> None:
            submitted.append(entity)

    monkeypatch.setenv("AZURE_TABLE_ENDPOINT", "https://acct.table.core.windows.net")
    monkeypatch.setattr(state_repo, "TableClient", RecordingTableClient)
    monkeypatch.setattr(state_repo, "get_credential", lambda: object())

    repo = JobStateRepository()
    repo.update("job-merge", task_id="celery-123")

    assert len(submitted) == 1
    patch = submitted[0]
    # Only the routing keys, the changed field, and the always-bumped
    # updated_at — NOT status/phase/payload_json from the stale snapshot.
    assert patch["PartitionKey"] == "job-merge"
    assert patch["RowKey"] == "current"
    assert patch["task_id"] == "celery-123"
    assert "updated_at" in patch
    assert "status" not in patch
    assert "phase" not in patch
    assert "payload_json" not in patch


def test_update_backfills_scope_columns_without_status(monkeypatch) -> None:
    """A scope-only ``update`` MUST patch just the scope columns + updated_at.

    This is the /v1/jobs cluster_name backfill path: the row already has the
    right status, we only fill the empty subscription/RG/cluster/storage
    columns so the AKS cluster card (which filters by cluster_name) shows the
    job under the same rule as Recent searches.
    """
    submitted: list[dict[str, object]] = []
    existing_entity = JobState(
        job_id="job-scope",
        type="blast",
        status="running",
        phase="running",
        owner_oid="owner-1",
    ).to_entity()

    class RecordingTableClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> RecordingTableClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def get_entity(self, *, partition_key: str, row_key: str) -> dict[str, object]:
            return dict(existing_entity)

        def update_entity(self, entity: dict[str, object], **_kwargs: object) -> None:
            submitted.append(entity)

    monkeypatch.setenv("AZURE_TABLE_ENDPOINT", "https://acct.table.core.windows.net")
    monkeypatch.setattr(state_repo, "TableClient", RecordingTableClient)
    monkeypatch.setattr(state_repo, "get_credential", lambda: object())

    repo = JobStateRepository()
    repo.update(
        "job-scope",
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
        cluster_name="elb-cluster-01",
    )

    assert len(submitted) == 1
    patch = submitted[0]
    assert patch["subscription_id"] == "sub-1"
    assert patch["resource_group"] == "rg-elb-cluster"
    assert patch["cluster_name"] == "elb-cluster-01"
    assert "updated_at" in patch
    # Status/phase untouched — a backfill must not rewrite the lifecycle.
    assert "status" not in patch
    assert "phase" not in patch


def test_update_explicit_scope_arg_wins_over_payload(monkeypatch) -> None:
    """When a caller passes BOTH ``payload`` and an explicit scope kwarg, the
    explicit value MUST win over the payload-derived canonical value.

    Guards the ordering: the payload-canonical block must run before the
    explicit scope writes so a future combined call cannot silently lose the
    explicit cluster_name.
    """
    submitted: list[dict[str, object]] = []
    existing_entity = JobState(
        job_id="job-both",
        type="blast",
        status="running",
        phase="running",
        owner_oid="owner-1",
    ).to_entity()

    class RecordingTableClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> RecordingTableClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def get_entity(self, *, partition_key: str, row_key: str) -> dict[str, object]:
            return dict(existing_entity)

        def update_entity(self, entity: dict[str, object], **_kwargs: object) -> None:
            submitted.append(entity)

    monkeypatch.setenv("AZURE_TABLE_ENDPOINT", "https://acct.table.core.windows.net")
    monkeypatch.setattr(state_repo, "TableClient", RecordingTableClient)
    monkeypatch.setattr(state_repo, "get_credential", lambda: object())

    repo = JobStateRepository()
    repo.update(
        "job-both",
        payload={"cluster_name": "payload-cluster", "job_title": "x"},
        cluster_name="explicit-cluster",
    )

    assert len(submitted) == 1
    patch = submitted[0]
    assert patch["cluster_name"] == "explicit-cluster"


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

        def query_entities(self, query_filter: str, *, results_per_page: int, **_kw):
            captured.append(query_filter)
            return []

    monkeypatch.setenv("AZURE_TABLE_ENDPOINT", "https://acct.table.core.windows.net")
    monkeypatch.setattr(state_repo, "TableClient", RecordingTableClient)
    monkeypatch.setattr(state_repo, "get_credential", lambda: object())

    repo = JobStateRepository()
    repo.list_completed(job_type="blast")

    assert captured == ["type eq 'blast' and status eq 'completed'"]


def test_list_completed_returns_newest_first_no_starvation(monkeypatch) -> None:
    """Regression: jobstate uses a random-uuid PartitionKey, so a single capped
    page returns an arbitrary fixed subset and completed jobs outside it were
    silently starved (never backfilled). ``list_completed`` must scan the full
    filtered set, sort by completion recency (``updated_at`` desc), and return
    the genuinely-newest ``limit`` rows with full payload re-fetched."""

    # 5 completed rows whose PartitionKey (uuid) order is DELIBERATELY the
    # reverse of their completion order, so a naive lexical-first page would
    # return the oldest-completed rows.
    summaries = [
        {
            "PartitionKey": f"{idx:08d}-uuid",
            "RowKey": "current",
            "type": "blast",
            "status": "completed",
            "updated_at": f"2026-06-1{idx}T00:00:00+00:00",
            "created_at": "2026-06-01T00:00:00+00:00",
        }
        # idx 1..5: larger idx == newer updated_at but larger PartitionKey.
        for idx in range(1, 6)
    ]
    full_by_pk = {
        s["PartitionKey"]: {**s, "payload_json": f'{{"job_id": "{s["PartitionKey"]}"}}'}
        for s in summaries
    }

    class RecordingTableClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> RecordingTableClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def query_entities(self, query_filter: str, *, results_per_page: int, **_kw):
            # Azure returns rows in (PartitionKey, RowKey) order — emulate that.
            return list(summaries)

        def get_entity(self, *, partition_key: str, row_key: str, **_kw):
            return dict(full_by_pk[partition_key])

    monkeypatch.setenv("AZURE_TABLE_ENDPOINT", "https://acct.table.core.windows.net")
    monkeypatch.setattr(state_repo, "TableClient", RecordingTableClient)
    monkeypatch.setattr(state_repo, "get_credential", lambda: object())

    repo = JobStateRepository()
    rows = repo.list_completed(job_type="blast", limit=3)

    # Newest 3 by updated_at, NOT the lexical-first 3 by PartitionKey.
    assert [r.job_id for r in rows] == ["00000005-uuid", "00000004-uuid", "00000003-uuid"]
    # Full payload was re-fetched for each returned row.
    assert all(r.payload for r in rows)


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
