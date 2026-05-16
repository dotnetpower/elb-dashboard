from __future__ import annotations

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
