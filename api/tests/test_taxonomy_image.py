"""Tests for Taxonomy Image behavior.

Responsibility: Tests for Taxonomy Image behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_install_transport`, `test_fetch_image_returns_thumbnail_url`,
`test_fetch_image_404_returns_null`, `test_fetch_image_swallows_network_errors`,
`test_fetch_image_caches_subsequent_lookups`, `test_fetch_image_rejects_off_origin_thumbnail`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_taxonomy_image.py`.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

_SUMMARY_RESPONSE = {
    "type": "standard",
    "title": "Human",
    "thumbnail": {
        "source": "https://upload.wikimedia.org/wikipedia/commons/thumb/6/68/Akha.jpg/330px-Akha.jpg",
        "width": 330,
        "height": 552,
    },
    "content_urls": {
        "desktop": {"page": "https://en.wikipedia.org/wiki/Human"},
        "mobile": {"page": "https://en.m.wikipedia.org/wiki/Human"},
    },
}


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    from api.services import taxonomy_image

    captured: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)
    original_client_cls = httpx.Client

    class _StubClient(original_client_cls):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(taxonomy_image.httpx, "Client", _StubClient)
    return captured


def test_fetch_image_returns_thumbnail_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import taxonomy_image

    taxonomy_image.clear_taxonomy_image_cache()
    requests = _install_transport(
        monkeypatch,
        lambda _req: httpx.Response(200, json=_SUMMARY_RESPONSE),
    )

    result = taxonomy_image.fetch_taxonomy_image("Homo sapiens")

    assert result["name"] == "Homo sapiens"
    assert result["image_url"].endswith("330px-Akha.jpg")
    assert result["page_url"] == "https://en.wikipedia.org/wiki/Human"
    assert result["source"] == "wikipedia"
    assert result["cached"] is False
    # Path is slugged with underscores and properly percent-encoded.
    assert requests[0].url.path == "/api/rest_v1/page/summary/Homo_sapiens"


def test_fetch_image_404_returns_null(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import taxonomy_image

    taxonomy_image.clear_taxonomy_image_cache()
    _install_transport(monkeypatch, lambda _req: httpx.Response(404, json={"detail": "missing"}))

    result = taxonomy_image.fetch_taxonomy_image("Notarealorganism")

    assert result["image_url"] is None
    assert result["page_url"] is None
    assert result["cached"] is False


def test_fetch_image_swallows_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import taxonomy_image

    taxonomy_image.clear_taxonomy_image_cache()

    def boom(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    _install_transport(monkeypatch, boom)

    result = taxonomy_image.fetch_taxonomy_image("Escherichia coli")

    assert result["image_url"] is None


def test_fetch_image_caches_subsequent_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import taxonomy_image

    taxonomy_image.clear_taxonomy_image_cache()
    requests = _install_transport(
        monkeypatch,
        lambda _req: httpx.Response(200, json=_SUMMARY_RESPONSE),
    )

    first = taxonomy_image.fetch_taxonomy_image("Homo sapiens")
    second = taxonomy_image.fetch_taxonomy_image("HOMO  SAPIENS")  # whitespace+case differs

    assert first["cached"] is False
    assert second["cached"] is True
    assert len(requests) == 1


def test_fetch_image_rejects_off_origin_thumbnail(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import taxonomy_image

    taxonomy_image.clear_taxonomy_image_cache()
    payload = {
        "thumbnail": {"source": "http://attacker.example/evil.jpg"},
        "content_urls": {"desktop": {"page": "https://attacker.example/wiki/x"}},
    }
    _install_transport(monkeypatch, lambda _req: httpx.Response(200, json=payload))

    result = taxonomy_image.fetch_taxonomy_image("Homo sapiens")

    assert result["image_url"] is None
    assert result["page_url"] is None


def test_fetch_image_caps_oversized_body(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import taxonomy_image

    taxonomy_image.clear_taxonomy_image_cache()
    big = b"a" * (taxonomy_image.MAX_BODY_BYTES + 1024)
    _install_transport(monkeypatch, lambda _req: httpx.Response(200, content=big))

    result = taxonomy_image.fetch_taxonomy_image("Homo sapiens")

    assert result["image_url"] is None


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "a" * 200,
        "Homo/sapiens",
        "Homo?sapiens",
        "Homo&sapiens",
        "../etc/passwd",
        "javascript:alert(1)",
    ],
)
def test_fetch_image_rejects_unsafe_names(bad: str) -> None:
    from api.services import taxonomy_image

    taxonomy_image.clear_taxonomy_image_cache()
    with pytest.raises(taxonomy_image.TaxonomyImageUnavailable):
        taxonomy_image.fetch_taxonomy_image(bad)


def test_image_route_returns_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import taxonomy_image

    def fake_fetch(name: str) -> dict[str, Any]:
        assert name == "Homo sapiens"
        return {
            "name": name,
            "image_url": "https://upload.wikimedia.org/x.jpg",
            "page_url": "https://en.wikipedia.org/wiki/Human",
            "source": "wikipedia",
            "cached": False,
        }

    monkeypatch.setattr(taxonomy_image, "fetch_taxonomy_image", fake_fetch)

    response = TestClient(app).get(
        "/api/blast/taxonomy/image",
        params={"name": "Homo sapiens"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["image_url"].endswith("x.jpg")


def test_image_route_rejects_bad_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    response = TestClient(app).get(
        "/api/blast/taxonomy/image",
        params={"name": "Homo/sapiens"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "taxonomy_image_invalid_name"
