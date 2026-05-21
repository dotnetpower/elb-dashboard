"""Tests for the NCBI catalogue cache helpers in api.routes.storage.common.

Responsibility: Verify that _resolve_latest_dir and _list_keys cache their
HTTP calls so prepare-db / warmup workflows don't re-hit NCBI per request.
Edit boundaries: Assertions focus on cache behaviour only; do not require live
network.
Key entry points: `test_resolve_latest_dir_caches_response`,
`test_list_keys_caches_by_latest_dir_and_db`,
`test_reset_ncbi_catalogue_cache_clears_both`
Risky contracts: Tests must monkey-patch httpx; never reach NCBI from CI.
Validation: `uv run pytest -q api/tests/test_storage_common_cache.py`.
"""

from __future__ import annotations

from xml.etree import ElementTree

from api.routes.storage import common


class _FakeResponse:
    def __init__(self, *, text: str = "", content: bytes = b"") -> None:
        self.text = text
        self.content = content

    def raise_for_status(self) -> None:
        return None


def _xml_with_keys(*keys: str) -> bytes:
    namespace = "http://s3.amazonaws.com/doc/2006-03-01/"
    root = ElementTree.Element(f"{{{namespace}}}ListBucketResult")
    for key in keys:
        contents = ElementTree.SubElement(root, f"{{{namespace}}}Contents")
        key_elem = ElementTree.SubElement(contents, f"{{{namespace}}}Key")
        key_elem.text = key
    truncated = ElementTree.SubElement(root, f"{{{namespace}}}IsTruncated")
    truncated.text = "false"
    return ElementTree.tostring(root)


def test_resolve_latest_dir_caches_response(monkeypatch) -> None:
    common.reset_ncbi_catalogue_cache()
    calls = {"n": 0}

    def fake_get(url, timeout=None):
        calls["n"] += 1
        return _FakeResponse(text="2026-05-01-01-05-02")

    import httpx

    monkeypatch.setattr(httpx, "get", fake_get)

    first = common._resolve_latest_dir()
    second = common._resolve_latest_dir()
    assert first == second == "2026-05-01-01-05-02"
    assert calls["n"] == 1


def test_list_keys_caches_by_latest_dir_and_db(monkeypatch) -> None:
    common.reset_ncbi_catalogue_cache()
    call_log: list[str] = []

    class _FakeClient:
        def __init__(self, *_a, **_kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def get(self, url):
            call_log.append(url)
            if "prefix=2026-05-01-01-05-02/core_nt" in url:
                return _FakeResponse(content=_xml_with_keys(
                    "2026-05-01-01-05-02/core_nt.000.nhr",
                    "2026-05-01-01-05-02/core_nt.000.nin",
                ))
            return _FakeResponse(content=_xml_with_keys())

    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    first = common._list_keys("2026-05-01-01-05-02", "core_nt")
    second = common._list_keys("2026-05-01-01-05-02", "core_nt")
    assert first == second
    assert len(first) == 2
    assert len(call_log) == 1

    # Different db -> separate cache entry, new request.
    third = common._list_keys("2026-05-01-01-05-02", "nt")
    assert third == []
    assert len(call_log) == 2


def test_reset_ncbi_catalogue_cache_clears_both(monkeypatch) -> None:
    common.reset_ncbi_catalogue_cache()
    calls = {"n": 0}

    def fake_get(url, timeout=None):
        calls["n"] += 1
        return _FakeResponse(text="v1")

    import httpx

    monkeypatch.setattr(httpx, "get", fake_get)
    common._resolve_latest_dir()
    common.reset_ncbi_catalogue_cache()
    common._resolve_latest_dir()
    assert calls["n"] == 2
