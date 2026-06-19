"""Tests for the NCBI nuccore search service + /api/ncbi/search,/features routes.

Responsibility: Cover ``search_nuccore`` (esearch → esummary mapping, relevance
order, term/limit validation) and ``fetch_feature_table`` (feature-table parse:
gene name, CDS product merge, plus/minus strand, coordinate normalisation,
limit cap), plus the two FastAPI routes (200 / 422 / dev-bypass auth). No live
network.
Edit boundaries: Only ``api/services/ncbi/search.py`` + ``api/routes/ncbi.py``.
Key entry points: ``search_nuccore``, ``fetch_feature_table``,
``/api/ncbi/search``, ``/api/ncbi/nuccore/{acc}/features``.
Risky contracts: NCBI HTTP is mocked via ``request_json`` / ``request_bytes``
monkeypatches on ``api.services.ncbi.search``.
Validation: ``uv run pytest -q api/tests/test_ncbi_search.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

_ESEARCH_JSON = {"esearchresult": {"idlist": ["111", "222"]}}
_ESUMMARY_JSON = {
    "result": {
        "uids": ["111", "222"],
        "111": {
            "accessionversion": "NC_063383.1",
            "title": "Monkeypox virus, complete genome",
            "organism": "Monkeypox virus",
            "taxid": 10244,
            "slen": 197209,
            "moltype": "dna",
            "biomol": "genomic",
            "sourcedb": "refseq",
            "status": "live",
        },
        "222": {
            "accessionversion": "OQ511287.1",
            "title": "Monkeypox virus isolate X",
            "organism": "Monkeypox virus",
            "taxid": 10244,
            "slen": 197000,
            "moltype": "dna",
            "sourcedb": "insd",
            "status": "live",
        },
    },
}

_FEATURE_TABLE = (
    ">Feature ref|NC_063383.1|\n"
    "1575\t835\tgene\n"
    "\t\t\tgene\tOPG001\n"
    "\t\t\tlocus_tag\tNBT03_gp001\n"
    "1575\t835\tCDS\n"
    "\t\t\tproduct\tPalmitylated EEV membrane protein\n"
    "100\t600\tgene\n"
    "\t\t\tgene\tOPGplus\n"
    "46483\t46022\tgene\n"
    "\t\t\tgene\tOPG057\n"
    "46483\t46022\tCDS\n"
    "\t\t\tproduct\tDNA-dependent RNA polymerase\n"
)


# ---------------------------------------------------------------------------
# search_nuccore (service)
# ---------------------------------------------------------------------------
def test_search_nuccore_maps_records(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services.ncbi import search as search_mod

    def fake_request_json(endpoint: str, params: dict[str, str]) -> dict[str, Any]:
        if endpoint == "esearch.fcgi":
            assert params["db"] == "nuccore"
            assert params["term"] == "monkeypox virus"
            return _ESEARCH_JSON
        assert endpoint == "esummary.fcgi"
        assert params["id"] == "111,222"
        return _ESUMMARY_JSON

    monkeypatch.setattr(search_mod, "request_json", fake_request_json)

    out = search_mod.search_nuccore("monkeypox virus", limit=5)

    assert out["count"] == 2
    assert [r["accession_version"] for r in out["results"]] == [
        "NC_063383.1",
        "OQ511287.1",
    ]
    first = out["results"][0]
    assert first["accession"] == "NC_063383"
    assert first["length"] == 197209
    assert first["is_refseq"] is True
    assert out["results"][1]["is_refseq"] is False


def test_search_nuccore_empty_idlist_skips_esummary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services.ncbi import search as search_mod

    calls: list[str] = []

    def fake_request_json(endpoint: str, _params: dict[str, str]) -> dict[str, Any]:
        calls.append(endpoint)
        return {"esearchresult": {"idlist": []}}

    monkeypatch.setattr(search_mod, "request_json", fake_request_json)
    out = search_mod.search_nuccore("no-such-organism-xyz")
    assert out["count"] == 0
    assert calls == ["esearch.fcgi"]  # esummary never called


@pytest.mark.parametrize("term", ["", "   ", "x" * 201])
def test_search_nuccore_rejects_bad_term(term: str) -> None:
    from api.services.ncbi import search as search_mod

    with pytest.raises(ValueError):
        search_mod.search_nuccore(term)


@pytest.mark.parametrize("limit", [0, 26, -1])
def test_search_nuccore_rejects_bad_limit(limit: int) -> None:
    from api.services.ncbi import search as search_mod

    with pytest.raises(ValueError):
        search_mod.search_nuccore("monkeypox virus", limit=limit)


# ---------------------------------------------------------------------------
# fetch_feature_table (service)
# ---------------------------------------------------------------------------
def test_fetch_feature_table_parses_genes(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services.ncbi import search as search_mod

    def fake_request_bytes(
        endpoint: str, params: dict[str, str], **_kw: Any
    ) -> bytes:
        assert endpoint == "efetch.fcgi"
        assert params["rettype"] == "ft"
        return _FEATURE_TABLE.encode("utf-8")

    monkeypatch.setattr(search_mod, "request_bytes", fake_request_bytes)

    out = search_mod.fetch_feature_table("NC_063383.1")
    feats = out["features"]
    assert out["count"] == 3
    # gene 1 — minus strand, coordinates normalised low..high, CDS product merged
    assert feats[0]["name"] == "OPG001"
    assert feats[0]["locus_tag"] == "NBT03_gp001"
    assert feats[0]["strand"] == "minus"
    assert feats[0]["start"] == 835
    assert feats[0]["stop"] == 1575
    assert feats[0]["length"] == 741
    assert feats[0]["product"] == "Palmitylated EEV membrane protein"
    # gene 2 — plus strand
    assert feats[1]["name"] == "OPGplus"
    assert feats[1]["strand"] == "plus"
    assert feats[1]["start"] == 100
    assert feats[1]["stop"] == 600
    assert feats[1]["length"] == 501
    # gene 3 — F3L-equivalent 462 bp span on minus strand
    assert feats[2]["name"] == "OPG057"
    assert feats[2]["length"] == 462
    assert feats[2]["product"] == "DNA-dependent RNA polymerase"


def test_fetch_feature_table_respects_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services.ncbi import search as search_mod

    monkeypatch.setattr(
        search_mod, "request_bytes", lambda *_a, **_kw: _FEATURE_TABLE.encode("utf-8")
    )
    out = search_mod.fetch_feature_table("NC_063383.1", limit=1)
    assert out["count"] == 1
    assert out["features"][0]["name"] == "OPG001"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def test_route_search_returns_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.routes.ncbi import _reset_caller_quota_for_tests
    from api.services.ncbi import search as search_mod

    _reset_caller_quota_for_tests()
    monkeypatch.setattr(
        search_mod,
        "search_nuccore",
        lambda term, limit=10: {"query": term, "count": 1, "results": [{"a": 1}]},
    )

    response = TestClient(app).get("/api/ncbi/search", params={"q": "monkeypox"})
    assert response.status_code == 200
    assert response.json()["count"] == 1


def test_route_search_requires_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    response = TestClient(app).get("/api/ncbi/search")
    assert response.status_code == 422


def test_route_features_returns_features(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.routes.ncbi import _reset_caller_quota_for_tests
    from api.services.ncbi import search as search_mod

    _reset_caller_quota_for_tests()
    monkeypatch.setattr(
        search_mod,
        "fetch_feature_table",
        lambda accession, limit=1000: {
            "accession_version": accession,
            "count": 1,
            "features": [{"name": "OPG057", "length": 462}],
        },
    )

    response = TestClient(app).get("/api/ncbi/nuccore/NC_063383.1/features")
    assert response.status_code == 200
    assert response.json()["features"][0]["name"] == "OPG057"


def test_route_features_rejects_bad_accession(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.routes.ncbi import _reset_caller_quota_for_tests

    _reset_caller_quota_for_tests()
    response = TestClient(app).get("/api/ncbi/nuccore/not-an-accession/features")
    assert response.status_code == 422


def test_route_features_too_many_is_friendly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chromosome-scale record (feature table over the cap) must return a
    friendly, actionable 422 rather than the generic 'response too large'."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.routes.ncbi import _reset_caller_quota_for_tests
    from api.services.ncbi import NcbiResponseTooLarge
    from api.services.ncbi import search as search_mod

    _reset_caller_quota_for_tests()

    def boom(accession: str, limit: int = 1000) -> dict:
        raise NcbiResponseTooLarge("NCBI response exceeded 6291456 byte limit")

    monkeypatch.setattr(search_mod, "fetch_feature_table", boom)
    response = TestClient(app).get("/api/ncbi/nuccore/AP019314.1/features")
    assert response.status_code == 422
    assert response.json()["code"] == "ncbi_features_too_many"
    assert "sub-range" in response.json()["message"]
