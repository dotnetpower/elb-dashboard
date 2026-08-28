"""Tests for immutable NCBI Direct transfer manifests.

Responsibility: Verify fixed-host URL validation, official MD5 parsing,
    provider-neutral generation identity, and fail-closed archive probing.
Edit boundaries: Network-free unit tests for `api.services.ncbi_direct` only.
Key entry points: Tests for `build_direct_manifest`.
Risky contracts: Missing checksums, unsafe URLs, duplicate archives, and invalid
    sizes must fail before any Kubernetes Job can be dispatched.
Validation: `uv run pytest -q api/tests/test_ncbi_direct.py`.
"""

import json

import httpx
import pytest
from api.routes.storage.common import NcbiUnavailable
from api.services.ncbi_direct import build_direct_manifest, reset_direct_manifest_cache


class _Client:
    def __init__(self, release: dict[str, object]) -> None:
        self.release = release

    def get(self, url: str) -> httpx.Response:
        request = httpx.Request("GET", url)
        if url.endswith("blastdb-metadata-1-1.json"):
            return httpx.Response(200, request=request, content=json.dumps([self.release]).encode())
        name = url.removesuffix(".md5").rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            request=request,
            content=f"0123456789abcdef0123456789abcdef  {name}\n".encode(),
        )

    def head(self, url: str) -> httpx.Response:
        return httpx.Response(
            200,
            request=httpx.Request("HEAD", url),
            headers={"Content-Length": "1024"},
        )


def _release(files: list[str] | None = None) -> dict[str, object]:
    return {
        "dbname": "core_nt",
        "last-updated": "2026-08-19T00:00:00",
        "number-of-letters": 998_069_435_926,
        "number-of-sequences": 130_155_243,
        "number-of-volumes": 2,
        "bytes-total": 2_048,
        "bytes-total-compressed": 2_048,
        "files": files
        or [
            "https://ftp.ncbi.nlm.nih.gov/blast/db/core_nt.00.tar.gz",
            "https://ftp.ncbi.nlm.nih.gov/blast/db/core_nt.01.tar.gz",
        ],
    }


def test_build_direct_manifest_pins_archives_and_generation() -> None:
    reset_direct_manifest_cache()
    manifest = build_direct_manifest("core_nt", client=_Client(_release()), ttl_seconds=0)

    assert manifest.generation_id.startswith("ncbi-direct-20260819-")
    assert len(manifest.release_fingerprint) == 64
    assert len(manifest.transfer_manifest_sha256) == 64
    assert [item.size for item in manifest.archives] == [1024, 1024]
    assert all(item.md5 == "0123456789abcdef0123456789abcdef" for item in manifest.archives)


def test_build_direct_manifest_normalizes_official_ftp_urls_to_https() -> None:
    reset_direct_manifest_cache()
    release = _release(["ftp://ftp.ncbi.nlm.nih.gov/blast/db/core_nt.00.tar.gz"])

    manifest = build_direct_manifest("core_nt", client=_Client(release), ttl_seconds=0)

    assert manifest.archives[0].url == ("https://ftp.ncbi.nlm.nih.gov/blast/db/core_nt.00.tar.gz")


def test_build_direct_manifest_rejects_untrusted_archive_host() -> None:
    reset_direct_manifest_cache()
    release = _release(["https://attacker.example/blast/db/core_nt.00.tar.gz"])

    with pytest.raises(NcbiUnavailable, match="unsafe archive URL"):
        build_direct_manifest("core_nt", client=_Client(release), ttl_seconds=0)


def test_build_direct_manifest_rejects_duplicate_archives() -> None:
    reset_direct_manifest_cache()
    url = "https://ftp.ncbi.nlm.nih.gov/blast/db/core_nt.00.tar.gz"

    with pytest.raises(NcbiUnavailable, match="duplicate archives"):
        build_direct_manifest("core_nt", client=_Client(_release([url, url])), ttl_seconds=0)
