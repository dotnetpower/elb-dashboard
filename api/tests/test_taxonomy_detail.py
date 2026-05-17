from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

# Minimal but representative efetch payload — exercises Lineage, LineageEx,
# OtherNames (multiple Name/ClassCDE shapes), ParentTaxId, GeneticCode,
# MitoGeneticCode, Division and the date fields.
_SAMPLE_XML = b"""<?xml version="1.0" ?>
<TaxaSet><Taxon>
  <TaxId>9606</TaxId>
  <ScientificName>Homo sapiens</ScientificName>
  <OtherNames>
    <GenbankCommonName>human</GenbankCommonName>
    <Synonym>Homo sapiens sapiens</Synonym>
    <Name>
      <ClassCDE>authority</ClassCDE>
      <DispName>Homo sapiens Linnaeus, 1758</DispName>
    </Name>
    <Name>
      <ClassCDE>misspelling</ClassCDE>
      <DispName>Home sapiens</DispName>
    </Name>
    <Name>
      <ClassCDE>misspelling</ClassCDE>
      <DispName>Homo sapien</DispName>
    </Name>
  </OtherNames>
  <ParentTaxId>9605</ParentTaxId>
  <Rank>species</Rank>
  <Division>Primates</Division>
  <GeneticCode>
    <GCId>1</GCId>
    <GCName>Standard</GCName>
  </GeneticCode>
  <MitoGeneticCode>
    <MGCId>2</MGCId>
    <MGCName>Vertebrate Mitochondrial</MGCName>
  </MitoGeneticCode>
  <Lineage>cellular organisms; Eukaryota; Metazoa; Chordata; Mammalia; Primates; Hominidae;
    Homo</Lineage>
  <LineageEx>
    <Taxon><TaxId>131567</TaxId><ScientificName>cellular organisms</ScientificName>
      <Rank>cellular root</Rank></Taxon>
    <Taxon><TaxId>2759</TaxId><ScientificName>Eukaryota</ScientificName>
      <Rank>domain</Rank></Taxon>
    <Taxon><TaxId>9605</TaxId><ScientificName>Homo</ScientificName>
      <Rank>genus</Rank></Taxon>
  </LineageEx>
  <CreateDate>1995/02/27 09:24:00</CreateDate>
  <UpdateDate>2024/09/10 00:00:00</UpdateDate>
</Taxon></TaxaSet>
"""


def test_fetch_taxonomy_detail_parses_full_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import taxonomy

    taxonomy.clear_taxonomy_detail_cache()
    calls: list[tuple[str, dict[str, str], int]] = []

    def fake_request_bytes(endpoint: str, params: dict[str, str], *, max_bytes: int) -> bytes:
        calls.append((endpoint, params, max_bytes))
        return _SAMPLE_XML

    monkeypatch.setattr(taxonomy, "_request_bytes", fake_request_bytes)

    result = taxonomy.fetch_taxonomy_detail(9606)

    assert calls == [
        (
            "efetch.fcgi",
            {"db": "taxonomy", "id": "9606", "retmode": "xml"},
            taxonomy.MAX_EFETCH_BYTES,
        )
    ]
    assert result["taxid"] == 9606
    assert result["scientific_name"] == "Homo sapiens"
    assert result["common_name"] == "human"
    assert result["rank"] == "species"
    assert result["division"] == "Primates"
    assert result["parent_taxid"] == 9605
    assert result["authority"] == "Homo sapiens Linnaeus, 1758"
    assert "Homo sapiens sapiens" in result["synonyms"]
    assert result["misspellings"] == ["Home sapiens", "Homo sapien"]
    assert result["lineage"].startswith("cellular organisms")
    assert len(result["lineage_ex"]) == 3
    assert result["lineage_ex"][2] == {"taxid": 9605, "scientific_name": "Homo", "rank": "genus"}
    assert result["genetic_code"] == "Standard"
    assert result["genetic_code_id"] == 1
    assert result["mito_genetic_code"] == "Vertebrate Mitochondrial"
    assert result["mito_genetic_code_id"] == 2
    assert result["create_date"].startswith("1995")
    assert result["update_date"].startswith("2024")
    assert result["cached"] is False
    assert result["source"] == "ncbi_eutils"


def test_fetch_taxonomy_detail_caches_second_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import taxonomy

    taxonomy.clear_taxonomy_detail_cache()
    invocations: list[int] = []

    def fake_request_bytes(_endpoint: str, _params: dict[str, str], *, max_bytes: int) -> bytes:
        invocations.append(max_bytes)
        return _SAMPLE_XML

    monkeypatch.setattr(taxonomy, "_request_bytes", fake_request_bytes)

    first = taxonomy.fetch_taxonomy_detail(9606)
    second = taxonomy.fetch_taxonomy_detail(9606)

    assert len(invocations) == 1
    assert first["cached"] is False
    assert second["cached"] is True
    # Returned dicts must not alias each other or the cached object.
    assert first is not second
    first["scientific_name"] = "tampered"
    third = taxonomy.fetch_taxonomy_detail(9606)
    assert third["scientific_name"] == "Homo sapiens"


@pytest.mark.parametrize("bad", [0, -1, 10**11, True, False, None, "abc", "12 34"])
def test_fetch_taxonomy_detail_rejects_bad_taxids(bad: Any) -> None:
    from api.services import taxonomy

    taxonomy.clear_taxonomy_detail_cache()
    with pytest.raises(ValueError):
        taxonomy.fetch_taxonomy_detail(bad)


def test_fetch_taxonomy_detail_rejects_xxe_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import taxonomy

    taxonomy.clear_taxonomy_detail_cache()
    xxe = (
        b"<?xml version=\"1.0\"?>\n"
        b"<!DOCTYPE foo [<!ENTITY xxe SYSTEM \"file:///etc/passwd\">]>\n"
        b"<TaxaSet><Taxon><TaxId>9606</TaxId>"
        b"<ScientificName>&xxe;</ScientificName></Taxon></TaxaSet>"
    )

    monkeypatch.setattr(
        taxonomy,
        "_request_bytes",
        lambda *_a, **_kw: xxe,
    )

    with pytest.raises(taxonomy.TaxonomySearchUnavailable):
        taxonomy.fetch_taxonomy_detail(9606)


def test_fetch_taxonomy_detail_handles_malformed_xml(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import taxonomy

    taxonomy.clear_taxonomy_detail_cache()
    monkeypatch.setattr(
        taxonomy,
        "_request_bytes",
        lambda *_a, **_kw: b"not xml at all",
    )

    with pytest.raises(taxonomy.TaxonomySearchUnavailable):
        taxonomy.fetch_taxonomy_detail(9606)


def test_fetch_taxonomy_detail_requires_taxon_element(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import taxonomy

    taxonomy.clear_taxonomy_detail_cache()
    monkeypatch.setattr(
        taxonomy,
        "_request_bytes",
        lambda *_a, **_kw: b"<?xml version=\"1.0\"?><TaxaSet></TaxaSet>",
    )

    with pytest.raises(taxonomy.TaxonomySearchUnavailable):
        taxonomy.fetch_taxonomy_detail(9606)


def test_taxonomy_detail_route_returns_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import taxonomy

    def fake_fetch(taxid: int) -> dict[str, Any]:
        assert taxid == 9606
        return {"taxid": 9606, "scientific_name": "Homo sapiens", "cached": False}

    monkeypatch.setattr(taxonomy, "fetch_taxonomy_detail", fake_fetch)

    response = TestClient(app).get("/api/blast/taxonomy/detail/9606")

    assert response.status_code == 200
    assert response.json()["scientific_name"] == "Homo sapiens"


def test_taxonomy_detail_route_rejects_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    response = TestClient(app).get("/api/blast/taxonomy/detail/0")

    assert response.status_code == 422
    # FastAPI body for path-validation failures.


def test_taxonomy_detail_route_maps_upstream_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import taxonomy

    def fake_fetch(_taxid: int) -> dict[str, Any]:
        raise taxonomy.TaxonomySearchUnavailable("boom")

    monkeypatch.setattr(taxonomy, "fetch_taxonomy_detail", fake_fetch)

    response = TestClient(app).get("/api/blast/taxonomy/detail/9606")

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "taxonomy_lookup_unavailable"
    assert body["retryable"] is True


def test_request_bytes_caps_response_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """The streaming reader must abort early if upstream sends more than the cap."""
    import httpx
    from api.services import taxonomy

    big_payload = b"a" * (taxonomy.MAX_EFETCH_BYTES + 2048)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big_payload)

    transport = httpx.MockTransport(handler)
    original_client_cls = httpx.Client

    class _StubClient(original_client_cls):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(taxonomy.httpx, "Client", _StubClient)

    with pytest.raises(taxonomy.TaxonomySearchUnavailable):
        taxonomy._request_bytes(
            "efetch.fcgi",
            {"db": "taxonomy", "id": "9606", "retmode": "xml"},
            max_bytes=taxonomy.MAX_EFETCH_BYTES,
        )
