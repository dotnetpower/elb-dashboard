"""Tests for Storage Data behavior.

Responsibility: Tests for Storage Data behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `FakeBlobClient`, `FakeBlobService`,
`test_upload_group_fasta_writes_queries_blob`, `test_upload_group_fasta_rejects_unsafe_paths`,
`FakeChunkDownload`, `FakeChunkBlobClient`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_storage_data.py`.
"""

from __future__ import annotations

import gzip
import json
from types import SimpleNamespace

import pytest
from api.services import storage_data


class FakeBlobClient:
    def __init__(self, url: str) -> None:
        self.url = url
        self.uploads: list[tuple[bytes, bool]] = []

    def upload_blob(self, data: bytes, *, overwrite: bool, **_kwargs: object) -> None:
        self.uploads.append((data, overwrite))


class FakeBlobService:
    def __init__(self) -> None:
        self.blobs: dict[tuple[str, str], FakeBlobClient] = {}

    def get_blob_client(self, container: str, blob_path: str) -> FakeBlobClient:
        key = (container, blob_path)
        if key not in self.blobs:
            self.blobs[key] = FakeBlobClient(
                f"https://elbstg01.blob.core.windows.net/{container}/{blob_path}"
            )
        return self.blobs[key]


def test_upload_group_fasta_writes_queries_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_service = FakeBlobService()
    monkeypatch.setattr(storage_data, "_blob_service", lambda *_args: fake_service)

    url = storage_data.upload_group_fasta(
        object(),
        "elbstg01",
        "split/job-123/qg1/query.fa",
        ">q1\nAAAA\n",
    )

    assert url == "https://elbstg01.blob.core.windows.net/queries/split/job-123/qg1/query.fa"
    blob = fake_service.blobs[("queries", "split/job-123/qg1/query.fa")]
    assert blob.uploads == [(b">q1\nAAAA\n", True)]


@pytest.mark.parametrize(
    "blob_path",
    ["../q.fa", "/split/job/q.fa", "split/job/q.fa?x=1", "split/job/q.fa#frag"],
)
def test_upload_group_fasta_rejects_unsafe_paths(blob_path: str) -> None:
    with pytest.raises(ValueError, match="invalid blob_path"):
        storage_data.upload_group_fasta(object(), "elbstg01", blob_path, ">q1\nAAAA\n")


def _storage_account(public: str, default_action: str, ip_rules: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        public_network_access=public,
        network_rule_set=SimpleNamespace(
            default_action=default_action,
            ip_rules=[SimpleNamespace(ip_address_or_range=ip) for ip in ip_rules],
        ),
    )


def test_classify_storage_failure_reports_selected_network_firewall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_client = SimpleNamespace(
        storage_accounts=SimpleNamespace(
            get_properties=lambda _rg, _account: _storage_account(
                "Enabled", "Deny", ["61.80.8.142"]
            )
        )
    )
    monkeypatch.setattr(
        "api.services.azure_clients.storage_client",
        lambda *_args: storage_client,
        raising=True,
    )
    monkeypatch.setattr(
        "api.services.storage_public_access._detect_caller_ip",
        lambda: "61.80.8.142",
        raising=True,
    )

    result = storage_data.classify_storage_failure(
        object(), "sub", "rg-elb-01", "elbstg01", RuntimeError("AuthorizationFailure")
    )

    assert result["degraded_reason"] == "firewall_blocked"
    assert result["public_access_disabled"] is False
    assert result["local_debug_access_blocked"] is True
    assert result["caller_ip_in_rules"] is True
    assert "selected networks" in result["message"]


def test_classify_storage_failure_reports_private_only_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_client = SimpleNamespace(
        storage_accounts=SimpleNamespace(
            get_properties=lambda _rg, _account: _storage_account("Disabled", "Deny", [])
        )
    )
    monkeypatch.setattr(
        "api.services.azure_clients.storage_client",
        lambda *_args: storage_client,
        raising=True,
    )

    result = storage_data.classify_storage_failure(
        object(), "sub", "rg-elb-01", "elbstg01", RuntimeError("AuthorizationFailure")
    )

    assert result["degraded_reason"] == "network_blocked"
    assert result["public_access_disabled"] is True


class FakeChunkDownload:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def chunks(self):
        midpoint = max(1, len(self.payload) // 2)
        yield self.payload[:midpoint]
        yield self.payload[midpoint:]


class FakeChunkBlobClient:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def download_blob(self) -> FakeChunkDownload:
        return FakeChunkDownload(self.payload)


class FakeChunkBlobService:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def get_blob_client(self, _container: str, _blob_path: str) -> FakeChunkBlobClient:
        return FakeChunkBlobClient(self.payload)


def test_read_result_blob_text_inflates_gzip_with_decompressed_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = gzip.compress(b"query\tresult\nsecond\trow\n")
    monkeypatch.setattr(
        storage_data,
        "_blob_service",
        lambda *_args: FakeChunkBlobService(payload),
    )

    text = storage_data.read_result_blob_text(
        object(), "elbstg01", "results", "job123/merged_results.out.gz", max_bytes=12
    )

    assert text == "query\tresult"


class FakeDownload:
    def __init__(self, payload: str) -> None:
        self.payload = payload

    def readall(self) -> bytes:
        return self.payload.encode("utf-8")


class FakeDownloadBlobClient:
    def __init__(self, payload: str) -> None:
        self.payload = payload

    def download_blob(self) -> FakeDownload:
        return FakeDownload(self.payload)


class FakeContainerClient:
    def __init__(self, blobs: list[SimpleNamespace], payloads: dict[str, str]) -> None:
        self.blobs = blobs
        self.payloads = payloads

    def list_blobs(self):
        return list(self.blobs)

    def get_blob_client(self, blob_name: str) -> FakeDownloadBlobClient:
        return FakeDownloadBlobClient(self.payloads[blob_name])


class FakeListBlobService:
    def __init__(self, container: FakeContainerClient) -> None:
        self.container = container

    def get_container_client(self, _container: str) -> FakeContainerClient:
        return self.container


def _blob(name: str, size: int = 1) -> SimpleNamespace:
    return SimpleNamespace(name=name, size=size, last_modified=None)


def test_list_databases_only_marks_verified_defaults_as_web_blast_searchsp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blobs = [
        _blob("core_nt.nsq"),
        _blob("core_nt-metadata.json"),
        _blob("custom_db/labdb/labdb.nsq"),
        _blob("custom_db/labdb/labdb-metadata.json"),
    ]
    payloads = {
        "core_nt-metadata.json": json.dumps({"effective_search_space": 111}),
        "custom_db/labdb/labdb-metadata.json": json.dumps({"effective_search_space": 222}),
    }
    fake_container = FakeContainerClient(blobs, payloads)
    monkeypatch.setattr(
        storage_data,
        "_blob_service",
        lambda *_args: FakeListBlobService(fake_container),
    )

    databases = {
        item["name"]: item for item in storage_data.list_databases(object(), "elbstg01", "blast-db")
    }

    assert databases["core_nt"]["web_blast_searchsp"] == 32_156_241_807_668
    assert databases["core_nt"]["db_effective_search_space"] == 111
    assert databases["core_nt"]["db_effective_search_space_source"] == "storage_metadata"
    assert databases["labdb"]["db_effective_search_space"] == 222
    assert "web_blast_searchsp" not in databases["labdb"]


def test_list_databases_reads_blastdb_json_display_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blobs = [
        _blob("core_nt/core_nt.nsq"),
        _blob("core_nt/core_nt.njs"),
    ]
    payloads = {
        "core_nt/core_nt.njs": json.dumps(
            {
                "title": "Core nucleotide BLAST database",
                "description": "A curated nucleotide collection",
                "dbtype": "Nucleotide",
                "last-updated": "2026-05-18T00:00:00Z",
                "number-of-sequences": 125_929_380,
                "number-of-letters": 1_234_567_890,
            }
        )
    }
    fake_container = FakeContainerClient(blobs, payloads)
    monkeypatch.setattr(
        storage_data,
        "_blob_service",
        lambda *_args: FakeListBlobService(fake_container),
    )

    databases = {
        item["name"]: item for item in storage_data.list_databases(object(), "elbstg01", "blast-db")
    }

    assert databases["core_nt"]["title"] == "Core nucleotide BLAST database"
    assert databases["core_nt"]["description"] == "A curated nucleotide collection"
    assert databases["core_nt"]["molecule_type"] == "Nucleotide"
    assert databases["core_nt"]["update_date"] == "2026-05-18T00:00:00Z"
    assert databases["core_nt"]["total_sequences"] == 125_929_380
    assert databases["core_nt"]["total_letters"] == 1_234_567_890


def test_list_databases_surfaces_db_order_oracle_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blobs = [
        _blob("core_nt.nsq"),
        _blob("metadata/oracles/core_nt/status.json"),
        _blob("metadata/oracles/core_nt/parts/run-1/00.txt"),
        _blob("metadata/oracles/core_nt/parts/run-1/01.txt"),
    ]
    payloads = {
        "metadata/oracles/core_nt/status.json": json.dumps(
            {
                "status": "building",
                "run_id": "run-1",
                "expected_parts": 2,
                "part_prefix": "metadata/oracles/core_nt/parts/run-1/",
            }
        )
    }
    fake_container = FakeContainerClient(blobs, payloads)
    monkeypatch.setattr(
        storage_data,
        "_blob_service",
        lambda *_args: FakeListBlobService(fake_container),
    )

    databases = {
        item["name"]: item for item in storage_data.list_databases(object(), "elbstg01", "blast-db")
    }

    assert databases["core_nt"]["db_order_oracle"] == {
        "status": "ready",
        "run_id": "run-1",
        "started_at": None,
        "source_version": None,
        "expected_parts": 2,
        "ready_parts": 2,
        "part_prefix": "metadata/oracles/core_nt/parts/run-1/",
    }


def test_list_databases_marks_db_order_oracle_stale_on_source_version_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blobs = [
        _blob("core_nt.nsq"),
        _blob("core_nt-metadata.json"),
        _blob("metadata/oracles/core_nt/status.json"),
        _blob("metadata/oracles/core_nt/parts/run-1/00.txt"),
    ]
    payloads = {
        "core_nt-metadata.json": json.dumps({"source_version": "new-snapshot"}),
        "metadata/oracles/core_nt/status.json": json.dumps(
            {
                "status": "building",
                "run_id": "run-1",
                "expected_parts": 1,
                "source_version": "old-snapshot",
                "part_prefix": "metadata/oracles/core_nt/parts/run-1/",
            }
        ),
    }
    fake_container = FakeContainerClient(blobs, payloads)
    monkeypatch.setattr(
        storage_data,
        "_blob_service",
        lambda *_args: FakeListBlobService(fake_container),
    )

    databases = {
        item["name"]: item for item in storage_data.list_databases(object(), "elbstg01", "blast-db")
    }

    assert databases["core_nt"]["source_version"] == "new-snapshot"
    assert databases["core_nt"]["db_order_oracle"]["status"] == "stale"


def test_list_databases_surfaces_update_and_shard_generation_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blobs = [
        _blob("core_nt.nsq"),
        _blob("core_nt-metadata.json"),
    ]
    payloads = {
        "core_nt-metadata.json": json.dumps(
            {
                "source_version": "2026-05-20-00-00-00",
                "sharded": True,
                "shard_sets": [10, "4", 4],
                "shard_source_version": "2026-05-19-00-00-00",
                "update_in_progress": True,
                "updating_to_source_version": "2026-05-21-00-00-00",
                "update_started_at": "2026-05-20T10:00:00+00:00",
                "update_error": "copy failed after retry",
                "update_failed_at": "2026-05-20T10:03:00+00:00",
            }
        )
    }
    fake_container = FakeContainerClient(blobs, payloads)
    monkeypatch.setattr(
        storage_data,
        "_blob_service",
        lambda *_args: FakeListBlobService(fake_container),
    )

    databases = {
        item["name"]: item for item in storage_data.list_databases(object(), "elbstg01", "blast-db")
    }

    core_nt = databases["core_nt"]
    assert core_nt["shard_sets"] == [4, 10]
    assert core_nt["shard_source_version"] == "2026-05-19-00-00-00"
    assert core_nt["shards_stale"] is True
    assert core_nt["update_in_progress"] is True
    assert core_nt["updating_to_source_version"] == "2026-05-21-00-00-00"
    assert core_nt["update_error"] == "copy failed after retry"


@pytest.mark.parametrize(
    "bad_name",
    [
        "Capitalized",  # uppercase not allowed
        "with-dash",  # hyphen not allowed
        "ab",  # too short
        "a" * 25,  # too long
        "victim.blob.core.windows.net",  # full-URL injection
        "",  # empty
        "name with space",
        "name?query=x",  # querystring injection
        "name/extra",  # path injection
    ],
)
def test_blob_service_rejects_invalid_account_names(bad_name: str) -> None:
    """Defence: storage_account is user-supplied querystring on every route.

    An invalid name must raise before the SDK builds a URL — otherwise a
    forged value like 'victim.blob.core.windows.net' would point the api
    sidecar's MI at an attacker-controlled host.
    """
    with pytest.raises(ValueError, match="invalid storage account name"):
        storage_data._blob_service(object(), bad_name)


def test_blob_service_accepts_valid_account_names() -> None:
    """Sanity: real-world account names pass our regex check.

    We only assert the validator behaviour — the SDK's own credential type
    check fires after that, which is fine.
    """
    assert storage_data._STORAGE_ACCOUNT_NAME_RE.fullmatch("elbstg01")
    assert storage_data._STORAGE_ACCOUNT_NAME_RE.fullmatch("abc")
    assert storage_data._STORAGE_ACCOUNT_NAME_RE.fullmatch("a" * 24)


class _FakeCredential:
    """Stand-in TokenCredential — BlobServiceClient only needs ``get_token`` to
    exist for construction; we never make a real network call in these tests.
    """

    def get_token(self, *_scopes, **_kw):  # pragma: no cover - never invoked
        raise AssertionError("test must not trigger token acquisition")


def test_blob_service_pool_returns_same_instance_for_same_account() -> None:
    """Hot path: repeated lookups MUST return the cached client so azure-core
    keeps its HTTP connection pool warm. Otherwise every list/download starts
    a new TLS handshake.
    """
    cred = _FakeCredential()
    storage_data.reset_blob_service_pool()
    first = storage_data._blob_service(cred, "elbstg01")
    second = storage_data._blob_service(cred, "elbstg01")
    assert first is second
    storage_data.reset_blob_service_pool()


def test_blob_service_pool_distinct_per_account_and_credential() -> None:
    """Key is (id(credential), account). Different account OR different
    credential object MUST yield a fresh client so we never reuse a token
    cache across identities.
    """
    cred1 = _FakeCredential()
    cred2 = _FakeCredential()
    storage_data.reset_blob_service_pool()
    a = storage_data._blob_service(cred1, "elbstg01")
    b = storage_data._blob_service(cred1, "elbstg02")
    c = storage_data._blob_service(cred2, "elbstg01")
    assert a is not b
    assert a is not c
    assert b is not c
    storage_data.reset_blob_service_pool()


def test_blob_service_pool_evicts_lru_when_over_capacity(monkeypatch) -> None:
    """LRU eviction guard: workloads that touch many accounts (migrations,
    multi-tenant inspections) MUST not grow the pool unboundedly. Oldest
    entry is closed and dropped first.
    """
    cred = _FakeCredential()
    storage_data.reset_blob_service_pool()
    monkeypatch.setattr(storage_data, "_BLOB_SERVICE_POOL_MAX", 2)
    closed: list[str] = []

    a = storage_data._blob_service(cred, "elbstg01")
    b = storage_data._blob_service(cred, "elbstg02")
    # Wrap close on a so we can assert it ran during eviction.
    original_close = a.close

    def _capture_close():
        closed.append("elbstg01")
        original_close()

    a.close = _capture_close  # type: ignore[method-assign]

    c = storage_data._blob_service(cred, "elbstg03")
    # a (oldest) should have been evicted and its close() invoked.
    assert closed == ["elbstg01"]
    assert b is not c
    storage_data.reset_blob_service_pool()


def test_reset_blob_service_pool_closes_clients() -> None:
    """reset_blob_service_pool must close every pooled client so the HTTP
    pool returns to the OS. Important on credential rotation in tests."""
    cred = _FakeCredential()
    storage_data.reset_blob_service_pool()
    client = storage_data._blob_service(cred, "elbstg01")
    closed: list[bool] = []
    original_close = client.close

    def _capture_close():
        closed.append(True)
        original_close()

    client.close = _capture_close  # type: ignore[method-assign]
    storage_data.reset_blob_service_pool()
    assert closed == [True]

