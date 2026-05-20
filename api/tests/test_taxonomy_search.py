"""Tests for Taxonomy Search behavior.

Responsibility: Tests for Taxonomy Search behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_summary_payload`, `test_taxonomy_search_name_uses_eutils_and_caches`,
`test_taxonomy_search_numeric_taxid_skips_esearch`,
`test_taxonomy_search_rejects_invalid_query`,
`test_taxonomy_search_rejects_invalid_numeric_taxid`, `test_taxonomy_search_rejects_bool_limit`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_taxonomy_search.py`.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _summary_payload() -> dict[str, object]:
    return {
        "result": {
            "uids": ["562"],
            "562": {
                "uid": "562",
                "scientificname": "Escherichia coli",
                "commonname": "E. coli",
                "rank": "species",
                "lineage": "cellular organisms; Bacteria; Pseudomonadota",
                "othernames": {"synonym": ["Bacterium coli"]},
            },
        }
    }


def test_taxonomy_search_name_uses_eutils_and_caches(monkeypatch) -> None:
    from api.services import taxonomy

    taxonomy.clear_taxonomy_cache()
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_request_json(endpoint: str, params: dict[str, str]) -> dict[str, object]:
        calls.append((endpoint, params))
        if endpoint == "esearch.fcgi":
            return {"esearchresult": {"idlist": ["562"]}}
        return _summary_payload()

    monkeypatch.setattr(taxonomy, "_request_json", fake_request_json)

    first = taxonomy.search_taxonomy("Escherichia coli", limit=5)
    second = taxonomy.search_taxonomy("Escherichia coli", limit=5)

    assert first["cached"] is False
    assert second["cached"] is True
    assert len(calls) == 2
    assert calls[0][0] == "esearch.fcgi"
    assert calls[0][1]["retmax"] == "5"
    assert first["results"][0]["taxid"] == 562
    assert first["results"][0]["scientific_name"] == "Escherichia coli"
    assert first["results"][0]["rank"] == "species"


def test_taxonomy_search_numeric_taxid_skips_esearch(monkeypatch) -> None:
    from api.services import taxonomy

    taxonomy.clear_taxonomy_cache()
    endpoints: list[str] = []

    def fake_request_json(endpoint: str, params: dict[str, str]) -> dict[str, object]:
        endpoints.append(endpoint)
        assert params["id"] == "562"
        return _summary_payload()

    monkeypatch.setattr(taxonomy, "_request_json", fake_request_json)

    payload = taxonomy.search_taxonomy("562")

    assert endpoints == ["esummary.fcgi"]
    assert payload["results"][0]["taxid"] == 562


def test_taxonomy_search_rejects_invalid_query() -> None:
    from api.services import taxonomy

    taxonomy.clear_taxonomy_cache()

    try:
        taxonomy.search_taxonomy("   ")
    except ValueError as exc:
        assert "required" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("blank taxonomy query should fail")


def test_taxonomy_search_rejects_invalid_numeric_taxid() -> None:
    from api.services import taxonomy

    taxonomy.clear_taxonomy_cache()

    try:
        taxonomy.search_taxonomy("0")
    except ValueError as exc:
        assert "positive integer" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("zero taxonomy taxid should fail")


def test_taxonomy_search_rejects_bool_limit() -> None:
    from api.services import taxonomy

    taxonomy.clear_taxonomy_cache()

    try:
        taxonomy.search_taxonomy("Escherichia coli", limit=True)
    except ValueError as exc:
        assert "limit must be an integer" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("boolean taxonomy search limit should fail")


def test_taxonomy_search_route_returns_results(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import taxonomy

    def fake_search_taxonomy(query: str, *, limit: int = 10) -> dict[str, object]:
        assert query == "E coli"
        assert limit == 3
        return {
            "query": query,
            "count": 1,
            "source": "ncbi_eutils",
            "cached": False,
            "results": [{"taxid": 562, "scientific_name": "Escherichia coli"}],
        }

    monkeypatch.setattr(taxonomy, "search_taxonomy", fake_search_taxonomy)

    response = TestClient(app).get("/api/blast/taxonomy/search", params={"q": "E coli", "limit": 3})

    assert response.status_code == 200
    assert response.json()["results"][0]["taxid"] == 562


def test_taxonomy_search_route_maps_upstream_errors(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import taxonomy

    def fake_search_taxonomy(_query: str, *, limit: int = 10) -> dict[str, object]:
        del limit
        raise taxonomy.TaxonomySearchUnavailable("NCBI taxonomy service is unavailable")

    monkeypatch.setattr(taxonomy, "search_taxonomy", fake_search_taxonomy)

    response = TestClient(app).get("/api/blast/taxonomy/search", params={"q": "E coli"})

    assert response.status_code == 503
    assert response.json()["code"] == "taxonomy_lookup_unavailable"
    assert response.json()["retryable"] is True
