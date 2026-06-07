"""Regression tests for ``shared_taxonomy_keys`` + prepare-db staging.

These guard the gap that produced ``TAXDB_SKIP`` warnings during warmup:
NCBI publishes ``taxdb.btd`` / ``taxdb.bti`` / ``taxonomy4blast.sqlite3`` at
the snapshot root (not under any per-DB prefix), so the prefix-based
``_list_keys`` would never pick them up and the warmup azcopy
``--include-pattern`` matched nothing.

Responsibility: Verify the HEAD-based discovery helper, its caching, its
    404-skip / 5xx / 403 failure modes, and that ``prepare-db`` appends the
    discovered keys to the copy plan (unless the feature flag is off).
Edit boundaries: Pure unit tests — monkeypatch httpx + httpx_pool, never
    reach NCBI or Azure Storage.
Key entry points: ``test_shared_taxonomy_keys_returns_only_existing``,
    ``test_shared_taxonomy_keys_caches_per_snapshot``,
    ``test_shared_taxonomy_keys_skips_individual_404``,
    ``test_shared_taxonomy_keys_raises_on_403``,
    ``test_shared_taxonomy_keys_raises_on_5xx``,
    ``test_prepare_db_feature_flag_off_skips_taxonomy``,
    ``test_prepare_db_appends_taxonomy_keys_to_copy_plan``.
Risky contracts: Empty results must NOT be cached — a transient NCBI 5xx
    cannot poison the next hour of prepare-db calls into a taxonomy-less
    state. The basename of each taxonomy key must end up at
    ``blast-db/<db>/<name>`` so the warmup script's existing
    ``--include-pattern`` finds them without modification.
Validation: ``uv run pytest -q api/tests/test_storage_shared_taxonomy.py``.
"""

from __future__ import annotations

import sys as _sys
from typing import Any, ClassVar

import api.routes.storage.prepare_db  # noqa: F401 — ensure submodule import
import pytest
from api.routes.storage import common
from api.routes.storage.common import (
    SHARED_TAXONOMY_FILES,
    NcbiAccessDenied,
    NcbiUnavailable,
    shared_taxonomy_keys,
)

prepare_db_module = _sys.modules["api.routes.storage.prepare_db"]


class _FakeHeadResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = ""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self, statuses: dict[str, int]) -> None:
        self._statuses = statuses
        self.head_calls: list[str] = []

    def head(self, url: str) -> _FakeHeadResponse:
        self.head_calls.append(url)
        # Match by suffix so tests can pass just the filename.
        for name, code in self._statuses.items():
            if url.endswith(name):
                return _FakeHeadResponse(code)
        return _FakeHeadResponse(404)


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    common.reset_ncbi_catalogue_cache()
    yield
    common.reset_ncbi_catalogue_cache()


def _install_fake_client(monkeypatch: pytest.MonkeyPatch, statuses: dict[str, int]) -> _FakeClient:
    fake = _FakeClient(statuses)
    monkeypatch.setattr(
        "api.services.httpx_pool.get_pooled_client",
        lambda *_a, **_kw: fake,
        raising=True,
    )
    return fake


def test_shared_taxonomy_files_constant_covers_all_three() -> None:
    assert set(SHARED_TAXONOMY_FILES) == {
        "taxdb.btd",
        "taxdb.bti",
        "taxonomy4blast.sqlite3",
    }


def test_shared_taxonomy_keys_returns_only_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_client(
        monkeypatch,
        {"taxdb.btd": 200, "taxdb.bti": 200, "taxonomy4blast.sqlite3": 200},
    )
    keys = shared_taxonomy_keys("2026-05-21-01-05-02")
    assert keys == [
        "2026-05-21-01-05-02/taxdb.btd",
        "2026-05-21-01-05-02/taxdb.bti",
        "2026-05-21-01-05-02/taxonomy4blast.sqlite3",
    ]


def test_shared_taxonomy_keys_skips_individual_404(monkeypatch: pytest.MonkeyPatch) -> None:
    # NCBI occasionally drops the sqlite while regenerating; the other two
    # must still be returned.
    _install_fake_client(
        monkeypatch,
        {"taxdb.btd": 200, "taxdb.bti": 200, "taxonomy4blast.sqlite3": 404},
    )
    keys = shared_taxonomy_keys("snap-2026")
    assert keys == ["snap-2026/taxdb.btd", "snap-2026/taxdb.bti"]


def test_shared_taxonomy_keys_caches_per_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_client(
        monkeypatch,
        {"taxdb.btd": 200, "taxdb.bti": 200, "taxonomy4blast.sqlite3": 200},
    )
    first = shared_taxonomy_keys("snap-A")
    second = shared_taxonomy_keys("snap-A")
    assert first == second
    # First call hits 3 HEADs, second call hits 0 (served from cache).
    assert len(fake.head_calls) == 3
    # Different snapshot dir bypasses the cache.
    shared_taxonomy_keys("snap-B")
    assert len(fake.head_calls) == 6


def test_shared_taxonomy_keys_empty_result_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All-404 means an empty list; a follow-up call must re-probe rather
    than serve a sticky empty result."""
    fake = _install_fake_client(monkeypatch, {})  # everything 404
    assert shared_taxonomy_keys("snap-C") == []
    assert shared_taxonomy_keys("snap-C") == []
    # No caching of negative results → 3 HEADs per call.
    assert len(fake.head_calls) == 6


def test_shared_taxonomy_keys_raises_on_403(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_client(monkeypatch, {"taxdb.btd": 403})
    with pytest.raises(NcbiAccessDenied):
        shared_taxonomy_keys("snap-D")


def test_shared_taxonomy_keys_raises_on_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_client(monkeypatch, {"taxdb.btd": 502})
    with pytest.raises(NcbiUnavailable):
        shared_taxonomy_keys("snap-E")


# ---------------------------------------------------------------------------
# prepare-db wiring
# ---------------------------------------------------------------------------


class _CapturingBlob:
    """Captures every start_copy_from_url(url) call for assertion."""

    started_urls: ClassVar[list[str]] = []
    started_targets: ClassVar[list[str]] = []

    def __init__(self, target_name: str) -> None:
        self._target_name = target_name

    def start_copy_from_url(self, url: str) -> None:
        _CapturingBlob.started_urls.append(url)
        _CapturingBlob.started_targets.append(self._target_name)


class _CapturingContainer:
    def __init__(self) -> None:
        self._meta: dict[str, Any] = {"db_name": "16S_ribosomal_RNA"}

    def get_blob_client(self, name: str) -> Any:
        if name.endswith("-metadata.json"):
            outer = self

            class _Meta:
                def download_blob(self, *, offset: int = 0, length: int | None = None) -> Any:
                    del offset, length
                    import json as _json

                    payload = _json.dumps(outer._meta).encode("utf-8")
                    return type(
                        "_S",
                        (),
                        {
                            "readall": lambda self: payload,
                            "properties": type("_P", (), {"etag": "etag-1"}),
                        },
                    )()

                def upload_blob(self, body: bytes, **_kw: Any) -> dict[str, str]:
                    import json as _json

                    outer._meta = _json.loads(body.decode("utf-8"))
                    return {"etag": '"etag-2"'}

            return _Meta()
        return _CapturingBlob(name)

    def list_blobs(self, name_starts_with: str | None = None, include: Any = None) -> list[Any]:
        del name_starts_with, include
        return []


class _CapturingBlobSvc:
    def __init__(self, container: _CapturingContainer) -> None:
        self._container = container

    def get_container_client(self, _name: str) -> _CapturingContainer:
        return self._container


@pytest.fixture()
def _client(monkeypatch: pytest.MonkeyPatch) -> Any:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.services.storage import prepare_db_locks as _locks

    with _locks._PREPARE_DB_LOCK_REGISTRY_GUARD:
        _locks._PREPARE_DB_LOCK_REGISTRY.clear()
    _CapturingBlob.started_urls = []
    _CapturingBlob.started_targets = []

    from api.main import app

    return TestClient(app)


def _patch_route_globals(
    monkeypatch: pytest.MonkeyPatch,
    *,
    snapshot: str,
    db_keys: list[str],
    tax_keys: list[str] | None,
) -> _CapturingContainer:
    monkeypatch.setattr(
        prepare_db_module, "_resolve_latest_dir", lambda: snapshot, raising=True
    )
    monkeypatch.setattr(
        prepare_db_module, "_list_keys", lambda _s, _d: list(db_keys), raising=True
    )
    # ``shared_taxonomy_keys`` is imported into the route module by name; patch
    # it on the module so the route's local binding picks up the fake.
    if tax_keys is None:
        def _raise(_s: str) -> list[str]:
            raise NcbiUnavailable("simulated outage")

        monkeypatch.setattr(prepare_db_module, "shared_taxonomy_keys", _raise, raising=True)
    else:
        monkeypatch.setattr(
            prepare_db_module,
            "shared_taxonomy_keys",
            lambda _s: list(tax_keys),
            raising=True,
        )
    monkeypatch.setattr(
        "api.services.storage.public_access.ensure_local_storage_access",
        lambda *_a, **_kw: {"action": "noop"},
        raising=True,
    )
    # Short-circuit the copy polling so the daemon thread finishes quickly.
    monkeypatch.setattr(
        prepare_db_module,
        "_poll_copy_completion",
        lambda *_a, **_kw: {
            "success": 0,
            "failed": 0,
            "aborted": 0,
            "pending": 0,
            "failed_files": [],
            "timed_out": False,
            "elapsed_seconds": 0,
        },
        raising=True,
    )
    monkeypatch.setattr(
        "api.services.db.ops_audit.record_db_op",
        lambda **_kw: "",
        raising=False,
    )
    container = _CapturingContainer()
    monkeypatch.setattr(
        "azure.storage.blob.BlobServiceClient",
        lambda **_kw: _CapturingBlobSvc(container),
    )
    monkeypatch.setattr(
        "api.services.storage.data._blob_service",
        lambda _cred, _account: _CapturingBlobSvc(container),
        raising=True,
    )
    return container


def _wait_for_daemon_completion(
    expected_count: int, timeout_seconds: float = 5.0
) -> None:
    """Wait until the prepare-db daemon thread has issued exactly
    ``expected_count`` ``start_copy_from_url`` calls. Polls every 25 ms and
    raises ``AssertionError`` on timeout so a CI-load flake fails loud
    instead of silently letting a stale assertion pass.

    The route launches ``_do_copies`` in a ``Thread(daemon=True)`` and the
    inner ``ThreadPoolExecutor(max_workers=20)`` typically finishes in
    well under 100 ms for the handful of fakes we register, but we still
    poll with a generous deadline because pytest-xdist + macOS CI can
    occasionally stall the scheduler.
    """
    import time as _time

    deadline = _time.monotonic() + timeout_seconds
    while _time.monotonic() < deadline:
        if len(_CapturingBlob.started_targets) >= expected_count:
            return
        _time.sleep(0.025)
    raise AssertionError(
        f"prepare-db daemon issued only {len(_CapturingBlob.started_targets)} "
        f"of expected {expected_count} copies within {timeout_seconds}s: "
        f"{_CapturingBlob.started_targets!r}"
    )


def test_prepare_db_appends_taxonomy_keys_to_copy_plan(
    monkeypatch: pytest.MonkeyPatch, _client: Any
) -> None:
    snapshot = "2026-05-21-01-05-02"
    _patch_route_globals(
        monkeypatch,
        snapshot=snapshot,
        db_keys=[f"{snapshot}/16S_ribosomal_RNA.nhr", f"{snapshot}/16S_ribosomal_RNA.nsq"],
        tax_keys=[
            f"{snapshot}/taxdb.btd",
            f"{snapshot}/taxdb.bti",
            f"{snapshot}/taxonomy4blast.sqlite3",
        ],
    )
    body = {
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "storage_resource_group": "rg-workload",
        "account_name": "stworkload",
        "db_name": "16S_ribosomal_RNA",
    }
    resp = _client.post("/api/storage/prepare-db", json=body)
    assert resp.status_code == 200, resp.text
    # Route reports the merged file count (2 DB + 3 taxonomy).
    assert resp.json()["files_total"] == 5

    _wait_for_daemon_completion(expected_count=5)
    # Each taxonomy file landed at blast-db/<db>/<basename>, NOT at the
    # snapshot-root path; the warmup azcopy --include-pattern looks for them
    # exactly there.
    assert sorted(_CapturingBlob.started_targets) == sorted(
        [
            "16S_ribosomal_RNA/16S_ribosomal_RNA.nhr",
            "16S_ribosomal_RNA/16S_ribosomal_RNA.nsq",
            "16S_ribosomal_RNA/taxdb.btd",
            "16S_ribosomal_RNA/taxdb.bti",
            "16S_ribosomal_RNA/taxonomy4blast.sqlite3",
        ]
    )
    # And the source URL for taxonomy is the snapshot root (not a per-DB
    # path) — i.e. NCBI's actual layout, not a fabricated one.
    assert any(
        url.endswith(f"/{snapshot}/taxdb.btd") for url in _CapturingBlob.started_urls
    )


def test_prepare_db_feature_flag_off_skips_taxonomy(
    monkeypatch: pytest.MonkeyPatch, _client: Any
) -> None:
    snapshot = "2026-05-21-01-05-02"
    monkeypatch.setattr(prepare_db_module, "_INCLUDE_SHARED_TAXONOMY", False, raising=True)
    container = _patch_route_globals(
        monkeypatch,
        snapshot=snapshot,
        db_keys=[f"{snapshot}/16S_ribosomal_RNA.nhr", f"{snapshot}/16S_ribosomal_RNA.nsq"],
        tax_keys=[f"{snapshot}/taxdb.btd"],  # would-be staged
    )
    del container  # we only need started_targets
    body = {
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "storage_resource_group": "rg-workload",
        "account_name": "stworkload",
        "db_name": "16S_ribosomal_RNA",
    }
    resp = _client.post("/api/storage/prepare-db", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["files_total"] == 2

    _wait_for_daemon_completion(expected_count=2)
    assert not any(
        t.endswith("taxdb.btd") for t in _CapturingBlob.started_targets
    ), f"taxonomy file leaked into copy plan with flag off: {_CapturingBlob.started_targets}"


def test_prepare_db_tolerates_taxonomy_probe_failure(
    monkeypatch: pytest.MonkeyPatch, _client: Any
) -> None:
    """An NCBI 5xx on the HEAD probe must NOT fail prepare-db. Only the
    taxonomy files are skipped; the per-DB files still go through."""
    snapshot = "2026-05-21-01-05-02"
    _patch_route_globals(
        monkeypatch,
        snapshot=snapshot,
        db_keys=[f"{snapshot}/16S_ribosomal_RNA.nhr", f"{snapshot}/16S_ribosomal_RNA.nsq"],
        tax_keys=None,  # → shared_taxonomy_keys raises NcbiUnavailable
    )
    body = {
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "storage_resource_group": "rg-workload",
        "account_name": "stworkload",
        "db_name": "16S_ribosomal_RNA",
    }
    resp = _client.post("/api/storage/prepare-db", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["files_total"] == 2

    _wait_for_daemon_completion(expected_count=2)
    assert not any(
        "taxdb" in t or "taxonomy4blast" in t
        for t in _CapturingBlob.started_targets
    )
