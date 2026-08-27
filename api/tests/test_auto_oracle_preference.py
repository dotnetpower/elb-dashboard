"""Tests for static Auto oracle preference persistence.

Responsibility: Verify validation, default-disabled compatibility, stable keys,
    and atomic local file round-trips without Azure access.
Edit boundaries: Preference service only; route dependency gates and build
    reconciliation have dedicated suites.
Key entry points: `test_missing_preference_is_disabled_by_absence`,
    `test_local_preference_round_trip`,
    `test_enabled_preference_requires_image_source`.
Risky contracts: Missing legacy rows must remain disabled and one DB/cluster
    key must not collide with another.
Validation: `uv run pytest -q api/tests/test_auto_oracle_preference.py`.
"""

from __future__ import annotations

import pytest
from api.services import auto_oracle
from api.services.auto_oracle import (
    auto_oracle_preference_key,
    get_auto_oracle_preference,
    get_auto_oracle_scan_cursor,
    list_auto_oracle_preference_page,
    list_auto_oracle_preferences,
    normalise_auto_oracle_preference,
    save_auto_oracle_preference,
    save_auto_oracle_scan_cursor,
)
from api.services.preference_concurrency import PreferenceUpdateConflict


def _value(**overrides: object) -> dict[str, object]:
    return {
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "cluster_resource_group": "rg-aks",
        "cluster_name": "aks-1",
        "storage_resource_group": "rg-storage",
        "storage_account": "stelbtest",
        "db_name": "core_nt",
        "acr_name": "acrelb",
        "enabled": True,
        **overrides,
    }


def test_missing_preference_is_disabled_by_absence(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))

    assert (
        get_auto_oracle_preference(
            "00000000-0000-0000-0000-000000000001",
            "rg-aks",
            "aks-1",
            "stelbtest",
            "core_nt",
        )
        is None
    )


def test_local_preference_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    pref = normalise_auto_oracle_preference(_value())

    save_auto_oracle_preference(pref)
    fetched = get_auto_oracle_preference(
        pref.subscription_id,
        pref.cluster_resource_group,
        pref.cluster_name,
        pref.storage_account,
        pref.db_name,
    )

    assert fetched == pref
    assert list_auto_oracle_preferences() == [pref]


def test_enabled_preference_requires_image_source() -> None:
    with pytest.raises(ValueError, match="acr_name or image"):
        normalise_auto_oracle_preference(_value(acr_name="", image="", enabled=True))
    disabled = normalise_auto_oracle_preference(_value(acr_name="", image="", enabled=False))
    assert disabled.enabled is False


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"acr_name": "bad-name"}, "acr_name"),
        ({"acr_name": "", "image": "bad image"}, "image reference"),
        ({"version": "x" * 1025}, "version is too large"),
    ],
)
def test_preference_rejects_unsafe_image_fields(overrides, message) -> None:
    with pytest.raises(ValueError, match=message):
        normalise_auto_oracle_preference(_value(**overrides))


def test_preference_key_changes_per_db_or_cluster() -> None:
    first = auto_oracle_preference_key("sub", "rg", "cluster-a", "storage", "core_nt")
    second = auto_oracle_preference_key("sub", "rg", "cluster-b", "storage", "core_nt")
    third = auto_oracle_preference_key("sub", "rg", "cluster-a", "storage", "nt")

    assert len({first, second, third}) == 3


def test_local_preference_create_only_and_stale_update_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    first = save_auto_oracle_preference(
        normalise_auto_oracle_preference(_value()),
        create_only=True,
    )

    with pytest.raises(PreferenceUpdateConflict):
        save_auto_oracle_preference(
            normalise_auto_oracle_preference(_value()),
            create_only=True,
        )
    with pytest.raises(PreferenceUpdateConflict):
        save_auto_oracle_preference(
            normalise_auto_oracle_preference(_value(version="stale-version", enabled=False))
        )

    updated = save_auto_oracle_preference(
        normalise_auto_oracle_preference(_value(version=first.etag, enabled=False))
    )
    assert updated.enabled is False
    assert updated.etag and updated.etag != first.etag


def test_local_pagination_reaches_preferences_beyond_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    rows: dict[str, object] = {}
    expected_names: set[str] = set()
    for index in range(505):
        db_name = f"db{index:03d}"
        preference = normalise_auto_oracle_preference(_value(db_name=db_name))
        rows[preference.key] = preference.to_dict()
        expected_names.add(db_name)
    monkeypatch.setattr(auto_oracle, "_read_file", lambda: rows)

    cursor = ""
    actual_names: set[str] = set()
    page_count = 0
    while True:
        page, cursor = list_auto_oracle_preference_page(
            limit=50,
            continuation_token=cursor,
            enabled_only=True,
        )
        page_count += 1
        actual_names.update(preference.db_name for preference in page)
        if not cursor:
            break

    assert page_count == 11
    assert actual_names == expected_names


def test_local_named_scan_cursors_are_independent(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))

    save_auto_oracle_scan_cursor("reconcile", "page-reconcile")
    save_auto_oracle_scan_cursor("retention", "page-retention")

    assert get_auto_oracle_scan_cursor("reconcile") == "page-reconcile"
    assert get_auto_oracle_scan_cursor("retention") == "page-retention"
    assert list_auto_oracle_preferences() == []


def test_table_pagination_round_trips_opaque_continuation_and_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = normalise_auto_oracle_preference(_value(db_name="db001"))
    second = normalise_auto_oracle_preference(_value(db_name="db002"))
    filters: list[str] = []

    class _Row(dict):
        def __init__(self, value) -> None:
            super().__init__(value)
            self.metadata = {"etag": "etag-page"}

    class _Pages:
        def __init__(self, token) -> None:
            self.token = token
            self.continuation_token = None
            self.used = False

        def __iter__(self):
            return self

        def __next__(self):
            if self.used:
                raise StopIteration
            self.used = True
            if self.token is None:
                self.continuation_token = {"next_partition_key": "page-2"}
                return [_Row(auto_oracle._entity(first))]
            assert self.token == {"next_partition_key": "page-2"}
            self.continuation_token = None
            return [_Row(auto_oracle._entity(second))]

    class _Paged:
        def by_page(self, *, continuation_token):
            return _Pages(continuation_token)

    class _Table:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def query_entities(self, query_filter, *, results_per_page):
            filters.append(query_filter)
            assert results_per_page == 1
            return _Paged()

    monkeypatch.setattr(auto_oracle, "_use_table_backend", lambda: True)
    monkeypatch.setattr(auto_oracle, "_ensure_table", lambda: None)
    monkeypatch.setattr(auto_oracle, "_table_client", lambda: _Table())

    page_one, cursor = list_auto_oracle_preference_page(
        limit=1,
        enabled_only=True,
        subscription_id=first.subscription_id,
        cluster_resource_group=first.cluster_resource_group,
        cluster_name=first.cluster_name,
        storage_account=first.storage_account,
    )
    page_two, final_cursor = list_auto_oracle_preference_page(
        limit=1,
        continuation_token=cursor,
        enabled_only=True,
        subscription_id=first.subscription_id,
        cluster_resource_group=first.cluster_resource_group,
        cluster_name=first.cluster_name,
        storage_account=first.storage_account,
    )

    assert [pref.db_name for pref in page_one] == ["db001"]
    assert page_one[0].etag == "etag-page"
    assert cursor
    assert [pref.db_name for pref in page_two] == ["db002"]
    assert final_cursor == ""
    assert len(filters) == 2
    assert all("status eq 'enabled'" in query for query in filters)
    assert all("storage_account eq 'stelbtest'" in query for query in filters)


def test_table_pagination_rejects_non_object_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auto_oracle, "_use_table_backend", lambda: True)
    monkeypatch.setattr(auto_oracle, "_ensure_table", lambda: None)
    scalar_cursor = auto_oracle._encode_continuation_token("not-an-object")

    with pytest.raises(ValueError, match="continuation token"):
        list_auto_oracle_preference_page(
            limit=1,
            continuation_token=scalar_cursor,
        )
