"""Tests for BLAST database Metadata behavior.

Responsibility: Tests for BLAST database Metadata behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_extract_db_name_handles_every_input_shape`,
`test_database_display_metadata_merges_core_nt_catalogue_with_storage_stats`,
`test_database_display_metadata_prefers_blastdb_metadata_title`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_blast_db_metadata.py`.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from api.services.blast.db_metadata import database_display_metadata_from_info, extract_db_name


def test_extract_db_name_handles_every_input_shape() -> None:
    assert extract_db_name("core_nt") == "core_nt"
    assert extract_db_name("blast-db/core_nt") == "core_nt"
    assert extract_db_name("blast-db/core_nt/core_nt") == "core_nt"
    assert (
        extract_db_name("https://elbstg01.blob.core.windows.net/blast-db/core_nt/core_nt")
        == "core_nt"
    )
    assert (
        extract_db_name("https://elbstg01.blob.core.windows.net/blast-db/core_nt/core_nt?ignored=1")
        == "core_nt"
    )
    assert extract_db_name("https://elbstg01.blob.core.windows.net/queries/q.fa") == ""
    assert extract_db_name("") == ""


def test_database_display_metadata_merges_core_nt_catalogue_with_storage_stats() -> None:
    metadata = database_display_metadata_from_info(
        "core_nt",
        {
            "source": "ncbi",
            "description": "Core nucleotide BLAST database",
            "source_version": "2026-05-18",
            "total_sequences": 125_929_380,
            "total_letters": 999_000_000,
        },
        fallback_database="https://elbstg01.blob.core.windows.net/blast-db/core_nt",
    )

    assert metadata["name"] == "core_nt"
    assert metadata["database"].endswith("/core_nt")
    assert metadata["title"] == "Core nucleotide BLAST database"
    assert metadata["description"].startswith("The core nucleotide BLAST database consists")
    assert metadata["molecule_type"] == "mixed DNA"
    assert metadata["update_date"] == "2026/05/18"
    assert metadata["number_of_sequences"] == 125_929_380
    assert metadata["number_of_letters"] == 999_000_000
    assert metadata["source_version"] == "2026-05-18"


def test_database_display_metadata_prefers_blastdb_metadata_title() -> None:
    metadata = database_display_metadata_from_info(
        "custom_db",
        {
            "title": "Lab isolates",
            "description": "Curated sequences",
            "molecule_type": "Nucleotide",
            "update_date": "2026/05/01",
            "number-of-sequences": "1,234",
        },
    )

    assert metadata["title"] == "Lab isolates"
    assert metadata["description"] == "Curated sequences"
    assert metadata["molecule_type"] == "mixed DNA"
    assert metadata["update_date"] == "2026/05/01"
    assert metadata["number_of_sequences"] == 1234


def test_resolve_database_display_metadata_caches_storage_lookups(monkeypatch) -> None:
    """Repeated calls for the same (storage_account, db) MUST avoid re-hitting
    Storage. Each job-detail page would otherwise trigger 2-4 blob downloads
    just to render the DB title / sequences / snapshot rows.
    """
    from api.services.blast import db_metadata as blast_db_metadata

    blastdb_calls = {"n": 0}
    storage_calls = {"n": 0}

    def fake_blastdb_json(storage_account: str, db_name: str):
        blastdb_calls["n"] += 1
        return {"title": "Core nucleotide BLAST database", "total_sequences": 100}

    def fake_storage(storage_account: str, db_name: str):
        storage_calls["n"] += 1
        return {"update_date": "2026/05/02"}

    monkeypatch.setattr(blast_db_metadata, "resolve_blastdb_json_metadata", fake_blastdb_json)
    monkeypatch.setattr(blast_db_metadata, "resolve_db_metadata", fake_storage)

    first = blast_db_metadata.resolve_database_display_metadata("elbstg01", "core_nt")
    second = blast_db_metadata.resolve_database_display_metadata("elbstg01", "core_nt")

    assert first is not None
    assert second == first
    assert blastdb_calls["n"] == 1
    assert storage_calls["n"] == 1


def test_invalidate_blast_db_metadata_cache_drops_one_db(monkeypatch) -> None:
    """``(account, db)`` invalidation MUST remove only that one key and let
    the next read re-fetch fresh metadata.
    """
    from api.services.blast import db_metadata as blast_db_metadata

    calls = {"n": 0}

    def fake_blastdb_json(_storage_account: str, _db_name: str):
        calls["n"] += 1
        return {"title": "v1"}

    monkeypatch.setattr(blast_db_metadata, "resolve_blastdb_json_metadata", fake_blastdb_json)
    monkeypatch.setattr(blast_db_metadata, "resolve_db_metadata", lambda *_a, **_kw: None)

    blast_db_metadata.resolve_database_display_metadata("elbstg01", "core_nt")
    blast_db_metadata.resolve_database_display_metadata("elbstg01", "nt")
    assert calls["n"] == 2

    removed = blast_db_metadata.invalidate_blast_db_metadata_cache("elbstg01", "core_nt")
    assert removed == 1

    # core_nt re-fetches, nt stays cached.
    blast_db_metadata.resolve_database_display_metadata("elbstg01", "core_nt")
    blast_db_metadata.resolve_database_display_metadata("elbstg01", "nt")
    assert calls["n"] == 3


def test_invalidate_blast_db_metadata_cache_drops_whole_account(monkeypatch) -> None:
    """``account`` only invalidation MUST sweep every db for that account but
    leave entries for other accounts untouched.
    """
    from api.services.blast import db_metadata as blast_db_metadata

    calls = {"n": 0}

    def fake_blastdb_json(_storage_account: str, _db_name: str):
        calls["n"] += 1
        return {"title": "v1"}

    monkeypatch.setattr(blast_db_metadata, "resolve_blastdb_json_metadata", fake_blastdb_json)
    monkeypatch.setattr(blast_db_metadata, "resolve_db_metadata", lambda *_a, **_kw: None)

    blast_db_metadata.resolve_database_display_metadata("elbstg01", "core_nt")
    blast_db_metadata.resolve_database_display_metadata("elbstg01", "nt")
    blast_db_metadata.resolve_database_display_metadata("elbstg02", "core_nt")
    assert calls["n"] == 3

    removed = blast_db_metadata.invalidate_blast_db_metadata_cache("elbstg01")
    assert removed == 2

    # elbstg01 entries re-fetch, elbstg02 stays cached.
    blast_db_metadata.resolve_database_display_metadata("elbstg01", "core_nt")
    blast_db_metadata.resolve_database_display_metadata("elbstg01", "nt")
    blast_db_metadata.resolve_database_display_metadata("elbstg02", "core_nt")
    assert calls["n"] == 5


def test_invalidate_blast_db_metadata_cache_global_clear(monkeypatch) -> None:
    """Calling with no args MUST clear every cache entry."""
    from api.services.blast import db_metadata as blast_db_metadata

    monkeypatch.setattr(
        blast_db_metadata, "resolve_blastdb_json_metadata", lambda *_a, **_kw: {"title": "v1"}
    )
    monkeypatch.setattr(blast_db_metadata, "resolve_db_metadata", lambda *_a, **_kw: None)

    blast_db_metadata.resolve_database_display_metadata("elbstg01", "core_nt")
    blast_db_metadata.resolve_database_display_metadata("elbstg02", "nt")
    removed = blast_db_metadata.invalidate_blast_db_metadata_cache()
    assert removed == 2


def test_publish_blast_db_metadata_invalidate_no_op_when_disabled(monkeypatch) -> None:
    """When ``BLAST_DB_METADATA_INVALIDATE_DISABLED=true`` (default in tests)
    publish MUST short-circuit and never call Redis. Production sets it to
    false / unsets it.
    """
    from api.services.blast import db_metadata as blast_db_metadata

    monkeypatch.setenv("BLAST_DB_METADATA_INVALIDATE_DISABLED", "true")

    class _ExplodingRedis:
        @staticmethod
        def from_url(*_a, **_kw):
            raise AssertionError("must not be reached when disabled")

    class _FakeRedisModule:
        Redis = _ExplodingRedis

    import sys

    monkeypatch.setitem(sys.modules, "redis", _FakeRedisModule)
    assert blast_db_metadata.publish_blast_db_metadata_invalidate("elbstg01", "nt") is False


def test_publish_blast_db_metadata_invalidate_calls_redis_publish(monkeypatch) -> None:
    """When enabled, publish MUST send a JSON payload on the channel."""
    from api.services.blast import db_metadata as blast_db_metadata

    monkeypatch.setenv("BLAST_DB_METADATA_INVALIDATE_DISABLED", "false")

    captured: dict[str, object] = {}

    class _FakeClient:
        def publish(self, channel: str, payload: bytes | str) -> int:
            captured["channel"] = channel
            captured["payload"] = payload
            return 1

    class _FakeRedisModule:
        Redis = type(
            "_R",
            (),
            {"from_url": staticmethod(lambda *_a, **_kw: _FakeClient())},
        )

    import sys

    monkeypatch.setitem(sys.modules, "redis", _FakeRedisModule)
    assert blast_db_metadata.publish_blast_db_metadata_invalidate("elbstg01", "core_nt") is True
    assert captured["channel"] == "elb:cache:blast-db-metadata"
    payload = captured["payload"]
    if isinstance(payload, bytes):
        payload = payload.decode()
    assert '"account":"elbstg01"' in payload
    assert '"db":"core_nt"' in payload


def test_notify_blast_db_metadata_changed_invalidates_locally_and_publishes(
    monkeypatch,
) -> None:
    """``notify_*`` MUST drop the local cache AND publish so peer sidecars
    drop theirs too.
    """
    from api.services.blast import db_metadata as blast_db_metadata

    monkeypatch.setattr(
        blast_db_metadata, "resolve_blastdb_json_metadata", lambda *_a, **_kw: {"title": "v1"}
    )
    monkeypatch.setattr(blast_db_metadata, "resolve_db_metadata", lambda *_a, **_kw: None)
    blast_db_metadata.resolve_database_display_metadata("elbstg01", "core_nt")

    published: list[tuple[str | None, str | None]] = []

    def _fake_publish(account, db):
        published.append((account, db))
        return True

    monkeypatch.setattr(blast_db_metadata, "publish_blast_db_metadata_invalidate", _fake_publish)
    blast_db_metadata.notify_blast_db_metadata_changed("elbstg01", "core_nt")
    assert published == [("elbstg01", "core_nt")]
    # Local cache is also empty for this key.
    assert (
        blast_db_metadata.invalidate_blast_db_metadata_cache("elbstg01", "core_nt") == 0
    )


def test_resolve_display_metadata_returns_independent_copy_per_call(monkeypatch) -> None:
    """Mutating the returned dict MUST NOT poison the cache for subsequent
    callers. Defensive deepcopy on hit / miss.
    """
    from api.services.blast import db_metadata as blast_db_metadata

    monkeypatch.setattr(
        blast_db_metadata,
        "resolve_blastdb_json_metadata",
        lambda *_a, **_kw: {"title": "v1", "total_sequences": 1},
    )
    monkeypatch.setattr(blast_db_metadata, "resolve_db_metadata", lambda *_a, **_kw: None)

    first = blast_db_metadata.resolve_database_display_metadata("elbstg01", "core_nt")
    assert first is not None
    first["mutated"] = True  # caller corruption simulation
    second = blast_db_metadata.resolve_database_display_metadata("elbstg01", "core_nt")
    assert second is not None
    assert "mutated" not in second


def test_resolve_display_metadata_single_flight_on_cache_miss(monkeypatch) -> None:
    """N concurrent callers on a cold cache MUST share one upstream lookup.
    Without single-flight, TTL boundary causes a thundering herd of Storage
    blob downloads.
    """
    from api.services.blast import db_metadata as blast_db_metadata

    lookup_calls = {"n": 0}
    lookup_started = threading.Event()
    release_lookup = threading.Event()

    def slow_lookup(_account, _db):
        lookup_calls["n"] += 1
        lookup_started.set()
        release_lookup.wait(timeout=5.0)
        return {"title": "v1"}

    monkeypatch.setattr(blast_db_metadata, "resolve_blastdb_json_metadata", slow_lookup)
    monkeypatch.setattr(blast_db_metadata, "resolve_db_metadata", lambda *_a, **_kw: None)

    results: list[Any] = []
    errors: list[BaseException] = []

    def worker():
        try:
            results.append(
                blast_db_metadata.resolve_database_display_metadata("elbstg01", "core_nt")
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    assert lookup_started.wait(timeout=3.0), "leader never started lookup"
    release_lookup.set()
    for t in threads:
        t.join(timeout=5.0)
        assert not t.is_alive(), "worker thread hung past leader release"

    assert not errors, f"unexpected errors: {errors}"
    assert len(results) == 8
    assert all(r == results[0] for r in results)
    assert lookup_calls["n"] == 1


def test_stop_invalidate_subscriber_signals_exit(monkeypatch) -> None:
    """The subscriber thread MUST honour stop_event within a couple of
    poll cycles (~1 s). Listen()'s old behaviour would block forever.
    """
    from api.services.blast import db_metadata as blast_db_metadata

    monkeypatch.setenv("BLAST_DB_METADATA_INVALIDATE_DISABLED", "false")

    class _FakePubSub:
        def __init__(self) -> None:
            self.subscribed: list[str] = []
            self.closed = False

        def subscribe(self, channel: str) -> None:
            self.subscribed.append(channel)

        def get_message(self, timeout: float = 1.0):
            time.sleep(0.05)
            return None

        def close(self) -> None:
            self.closed = True

    class _FakeClient:
        def pubsub(self, ignore_subscribe_messages: bool = True):
            return _FakePubSub()

    class _FakeRedisModule:
        Redis = type(
            "_R",
            (),
            {"from_url": staticmethod(lambda *_a, **_kw: _FakeClient())},
        )

    import sys

    monkeypatch.setitem(sys.modules, "redis", _FakeRedisModule)
    thread = blast_db_metadata.start_invalidate_subscriber()
    assert thread is not None
    time.sleep(0.3)
    blast_db_metadata.stop_invalidate_subscriber(timeout=3.0)
    assert not thread.is_alive(), "subscriber thread did not exit after stop"

