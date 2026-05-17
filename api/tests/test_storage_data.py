from __future__ import annotations

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
