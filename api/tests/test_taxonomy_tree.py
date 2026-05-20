"""Tests for `fetch_taxonomy_tree` and the GET /blast/taxonomy/tree route.

Responsibility: Tests for `fetch_taxonomy_tree` and the GET /blast/taxonomy/tree route
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_reset_caches`, `_install_fakes`,
`test_fetch_taxonomy_tree_groups_siblings_by_parent`,
`test_fetch_taxonomy_tree_uses_cache_on_second_call`,
`test_fetch_taxonomy_tree_cache_key_includes_limit`,
`test_taxonomy_tree_route_maps_bad_taxid_to_422`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_taxonomy_tree.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

# Minimal efetch payload — just enough lineage for two major ranks to be
# queried (phylum -> class -> family -> genus -> species). We keep it
# small to keep test setup readable.
_SAMPLE_XML = b"""<?xml version="1.0" ?>
<TaxaSet><Taxon>
  <TaxId>9606</TaxId>
  <ScientificName>Homo sapiens</ScientificName>
  <ParentTaxId>9605</ParentTaxId>
  <Rank>species</Rank>
  <Lineage>cellular organisms; Eukaryota; Metazoa; Chordata; Mammalia; Hominidae; Homo</Lineage>
  <LineageEx>
    <Taxon><TaxId>33208</TaxId><ScientificName>Metazoa</ScientificName>
      <Rank>kingdom</Rank></Taxon>
    <Taxon><TaxId>7711</TaxId><ScientificName>Chordata</ScientificName>
      <Rank>phylum</Rank></Taxon>
    <Taxon><TaxId>40674</TaxId><ScientificName>Mammalia</ScientificName>
      <Rank>class</Rank></Taxon>
    <Taxon><TaxId>9604</TaxId><ScientificName>Hominidae</ScientificName>
      <Rank>family</Rank></Taxon>
    <Taxon><TaxId>9605</TaxId><ScientificName>Homo</ScientificName>
      <Rank>genus</Rank></Taxon>
  </LineageEx>
</Taxon></TaxaSet>
"""


def _reset_caches(taxonomy) -> None:
    taxonomy.clear_taxonomy_cache()
    taxonomy.clear_taxonomy_detail_cache()
    taxonomy.clear_taxonomy_siblings_cache()


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sibling_payload_by_term: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, list[Any]]:
    """Patch network helpers and return a counters dict for assertions."""
    from api.services import taxonomy

    counters: dict[str, list[Any]] = {
        "efetch": [],
        "esearch": [],
        "esummary_terms": [],
    }

    def fake_request_bytes(endpoint: str, params: dict[str, str], *, max_bytes: int) -> bytes:
        counters["efetch"].append((endpoint, params, max_bytes))
        return _SAMPLE_XML

    monkeypatch.setattr(taxonomy, "_request_bytes", fake_request_bytes)

    sibling_payload_by_term = sibling_payload_by_term or {}

    def fake_search_taxids(query: str, limit: int) -> list[str]:
        counters["esearch"].append((query, limit))
        payload = sibling_payload_by_term.get(query, [])
        return [str(row["taxid"]) for row in payload]

    def fake_summarise_taxids(taxids: list[str], *, query: str) -> list[dict[str, Any]]:
        counters["esummary_terms"].append((tuple(taxids), query))
        # Reverse lookup by taxid against any payload we know about.
        flat = {str(row["taxid"]): row for rows in sibling_payload_by_term.values() for row in rows}
        out: list[dict[str, Any]] = []
        for tid in taxids:
            row = flat.get(tid)
            if row is not None:
                out.append(
                    {
                        "taxid": int(row["taxid"]),
                        "scientific_name": row["scientific_name"],
                        "rank": row["rank"],
                    }
                )
        return out

    monkeypatch.setattr(taxonomy, "_search_taxids", fake_search_taxids)
    monkeypatch.setattr(taxonomy, "_summarise_taxids", fake_summarise_taxids)

    return counters


def test_fetch_taxonomy_tree_groups_siblings_by_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services import taxonomy

    _reset_caches(taxonomy)
    counters = _install_fakes(
        monkeypatch,
        sibling_payload_by_term={
            # Sibling phyla under Metazoa
            'txid33208[Subtree] AND "phylum"[Rank]': [
                {"taxid": 7711, "scientific_name": "Chordata", "rank": "phylum"},
                {"taxid": 6447, "scientific_name": "Mollusca", "rank": "phylum"},
                {"taxid": 6157, "scientific_name": "Platyhelminthes", "rank": "phylum"},
                {"taxid": 6231, "scientific_name": "Nematoda", "rank": "phylum"},
            ],
            # Sibling classes under Chordata
            'txid7711[Subtree] AND "class"[Rank]': [
                {"taxid": 40674, "scientific_name": "Mammalia", "rank": "class"},
                {"taxid": 8782, "scientific_name": "Aves", "rank": "class"},
                {"taxid": 7898, "scientific_name": "Actinopterygii", "rank": "class"},
            ],
            # Sibling families under Mammalia
            'txid40674[Subtree] AND "family"[Rank]': [
                {"taxid": 9604, "scientific_name": "Hominidae", "rank": "family"},
                {"taxid": 9681, "scientific_name": "Felidae", "rank": "family"},
            ],
            # Sibling genera under Hominidae (only Pan beyond Homo)
            'txid9604[Subtree] AND "genus"[Rank]': [
                {"taxid": 9605, "scientific_name": "Homo", "rank": "genus"},
                {"taxid": 9596, "scientific_name": "Pan", "rank": "genus"},
            ],
            # Sibling species under Homo (only the focal Homo sapiens)
            'txid9605[Subtree] AND "species"[Rank]': [
                {"taxid": 9606, "scientific_name": "Homo sapiens", "rank": "species"},
            ],
        },
    )

    result = taxonomy.fetch_taxonomy_tree(9606, sibling_limit=3)

    assert result["taxid"] == 9606
    assert result["scientific_name"] == "Homo sapiens"
    assert result["source"] == "ncbi_eutils"
    # `lineage` must contain the LineageEx entries plus the focal taxon itself.
    lineage_taxids = [n["taxid"] for n in result["lineage"]]
    assert lineage_taxids[-1] == 9606
    assert lineage_taxids[:5] == [33208, 7711, 40674, 9604, 9605]

    # Siblings are keyed by parent taxid (as strings).
    siblings = result["siblings"]
    assert set(siblings.keys()) == {"33208", "7711", "40674", "9604"}
    # "9605" group only contained Homo sapiens (the focal taxon) and gets
    # filtered to empty -> not added to the map.
    assert "9605" not in siblings

    # exclude_taxid: the focal lineage child should NOT appear in its rank's
    # sibling list. e.g. Chordata is excluded from the phylum sibling group.
    phylum_taxids = {s["taxid"] for s in siblings["33208"]}
    assert 7711 not in phylum_taxids
    # The first call hits the network once per major rank that has a parent.
    assert len(counters["esearch"]) == 5  # phylum, class, family, genus, species
    # cached=False on first call because none of the sibling lookups were cached
    assert result["cached"] is False


def test_fetch_taxonomy_tree_uses_cache_on_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services import taxonomy

    _reset_caches(taxonomy)
    counters = _install_fakes(
        monkeypatch,
        sibling_payload_by_term={
            'txid33208[Subtree] AND "phylum"[Rank]': [
                {"taxid": 7711, "scientific_name": "Chordata", "rank": "phylum"},
                {"taxid": 6447, "scientific_name": "Mollusca", "rank": "phylum"},
            ],
        },
    )

    first = taxonomy.fetch_taxonomy_tree(9606, sibling_limit=3)
    esearch_after_first = len(counters["esearch"])
    assert esearch_after_first >= 1

    second = taxonomy.fetch_taxonomy_tree(9606, sibling_limit=3)
    # Second call must be served entirely from cache (no extra esearch).
    assert len(counters["esearch"]) == esearch_after_first
    # And it must surface that fact via the `cached` flag.
    assert second["cached"] is True
    # Payloads must be equivalent (siblings + lineage).
    assert second["lineage"] == first["lineage"]
    assert second["siblings"] == first["siblings"]


def test_fetch_taxonomy_tree_cache_key_includes_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wider sibling_limit must re-query rather than reuse a small cached page."""
    from api.services import taxonomy

    _reset_caches(taxonomy)
    counters = _install_fakes(
        monkeypatch,
        sibling_payload_by_term={
            'txid33208[Subtree] AND "phylum"[Rank]': [
                {"taxid": 7711, "scientific_name": "Chordata", "rank": "phylum"},
                {"taxid": 6447, "scientific_name": "Mollusca", "rank": "phylum"},
                {"taxid": 6231, "scientific_name": "Nematoda", "rank": "phylum"},
            ],
        },
    )

    taxonomy.fetch_taxonomy_tree(9606, sibling_limit=1)
    calls_after_small = len(counters["esearch"])
    taxonomy.fetch_taxonomy_tree(9606, sibling_limit=5)
    # Different limit -> different cache key -> at least one more esearch.
    assert len(counters["esearch"]) > calls_after_small


def test_taxonomy_tree_route_maps_bad_taxid_to_422(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import taxonomy

    _reset_caches(taxonomy)
    # Path validation rejects taxid=0 before the handler is reached, so this
    # is really a smoke test of the Path(..., ge=1) constraint.
    client = TestClient(app)
    response = client.get("/api/blast/taxonomy/tree/0?sibling_limit=3")
    assert response.status_code == 422


def test_taxonomy_tree_route_503_when_upstream_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import taxonomy

    _reset_caches(taxonomy)

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise taxonomy.TaxonomySearchUnavailable("upstream down")

    monkeypatch.setattr(taxonomy, "fetch_taxonomy_tree", boom)

    client = TestClient(app)
    response = client.get("/api/blast/taxonomy/tree/9606?sibling_limit=3")
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "taxonomy_tree_unavailable"
    assert body["retryable"] is True
    assert body["retry_after_seconds"] == 30
