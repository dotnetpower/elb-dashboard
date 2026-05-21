"""Tests for blast_result_analytics organism extraction helpers.

Responsibility: Lock in the `extract_organism_from_stitle` heuristic,
the `rollup_taxonomy` stitle-fallback behaviour, and the
`enrich_taxonomy_with_lineage` name→taxid resolution + blast_name
derivation, so regressions surface immediately.
Edit boundaries: Pure functions only; do not touch storage_data here.
Key entry points: `test_extract_organism_*`, `test_rollup_taxonomy_*`,
`test_enrich_taxonomy_*`.
Risky contracts: The heuristic feeds a server-side rollup that the SPA
renders without further filtering — keep false positives out by
preferring "" over mis-classification.
Validation: `uv run pytest -q api/tests/test_blast_result_analytics_organism.py`.
"""

from __future__ import annotations

import pytest
from api.services import taxonomy as taxonomy_service
from api.services.blast_result_analytics import (
    enrich_taxonomy_with_lineage,
    extract_organism_from_stitle,
    rollup_taxonomy,
)


@pytest.mark.parametrize(
    "stitle,expected",
    [
        (
            "Monkeypox virus isolate 24MPX2634V genome assembly, complete genome",
            "Monkeypox virus",
        ),
        ("Homo sapiens chromosome 7, GRCh38 reference", "Homo sapiens"),
        ("Escherichia coli strain K-12 complete genome", "Escherichia coli"),
        (
            "Severe acute respiratory syndrome coronavirus 2 isolate Wuhan-Hu-1",
            "Severe acute respiratory syndrome coronavirus 2",
        ),
        (
            "PREDICTED: Mus musculus uncharacterized LOC123 (Loc123), mRNA",
            "Mus musculus uncharacterized LOC123",
        ),
        (
            "Saccharomyces cerevisiae S288C chromosome IV, complete sequence",
            "Saccharomyces cerevisiae S288C",
        ),
        ("Drosophila melanogaster", "Drosophila melanogaster"),
        ("", ""),
        ("   ", ""),
        # No confident candidate — too many tokens.
        (
            "Some very long marketing tagline with no scientific name at all that goes on and on",
            "",
        ),
    ],
)
def test_extract_organism_from_stitle(stitle: str, expected: str) -> None:
    assert extract_organism_from_stitle(stitle) == expected


def test_rollup_taxonomy_uses_stitle_fallback_when_sscinames_missing() -> None:
    """When sscinames/staxids are absent but stitle is parseable, the
    rollup keys on the extracted organism instead of dumping every hit
    into the `unclassified` bucket."""
    hits = [
        {"stitle": "Monkeypox virus isolate A genome", "evalue": 1e-50, "bitscore": 828},
        {"stitle": "Monkeypox virus isolate B genome", "evalue": 2e-50, "bitscore": 820},
        {"stitle": "Homo sapiens chromosome 1", "evalue": 1e-10, "bitscore": 120},
    ]
    rows = rollup_taxonomy(hits)
    assert len(rows) == 2
    by_organism = {row["organism"]: row for row in rows}
    assert by_organism["Monkeypox virus"]["count"] == 2
    assert by_organism["Monkeypox virus"]["organism_source"] == "stitle"
    assert by_organism["Monkeypox virus"]["taxid"] == ""
    assert by_organism["Homo sapiens"]["count"] == 1
    assert by_organism["Homo sapiens"]["organism_source"] == "stitle"


def test_rollup_taxonomy_prefers_sscinames_over_stitle_fallback() -> None:
    """`sscinames` always wins over the heuristic when present — even if
    the heuristic would have produced a different bucket."""
    hits = [
        {
            "sscinames": "Monkeypox virus",
            "staxids": "10244",
            "stitle": "Some misleading title chromosome 7",
            "evalue": 1e-50,
            "bitscore": 828,
        },
    ]
    rows = rollup_taxonomy(hits)
    assert len(rows) == 1
    assert rows[0]["organism"] == "Monkeypox virus"
    assert rows[0]["taxid"] == "10244"
    assert rows[0]["organism_source"] == "sscinames"


def test_rollup_taxonomy_keeps_unclassified_when_stitle_unparseable() -> None:
    """If the heuristic returns "" we keep the old `unclassified` bucket
    rather than mislabel rows."""
    hits = [
        {"stitle": "", "evalue": 1e-10, "bitscore": 100},
        {"stitle": "12345", "evalue": 1e-5, "bitscore": 50},
    ]
    rows = rollup_taxonomy(hits)
    assert len(rows) == 1
    assert rows[0]["key"] == "unclassified"
    assert rows[0]["organism"] == ""
    assert rows[0]["count"] == 2


def test_enrich_taxonomy_resolves_organism_name_when_taxid_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 2a: when the rollup row has an organism name (from the
    stitle fallback) but no taxid, the enricher calls `search_taxonomy`
    to resolve the name → taxid before fetching the lineage detail."""
    taxonomy_service.clear_taxonomy_cache()

    def fake_search(query: str, *, limit: int = 10):
        assert query == "Monkeypox virus"
        return {
            "query": query,
            "count": 1,
            "source": "test",
            "cached": False,
            "results": [
                {
                    "taxid": 10244,
                    "scientific_name": "Monkeypox virus",
                    "lineage": "Viruses; ...; Orthopoxvirus",
                }
            ],
        }

    def fake_detail(taxid: int):
        assert taxid == 10244
        return {
            "taxid": 10244,
            "scientific_name": "Monkeypox virus",
            "lineage": "Viruses; Varidnaviria; Orthopoxvirus",
            "lineage_ex": [
                {"rank": "superkingdom", "taxid": 10239, "scientific_name": "Viruses"},
                {"rank": "genus", "taxid": 10240, "scientific_name": "Orthopoxvirus"},
            ],
        }

    monkeypatch.setattr(taxonomy_service, "search_taxonomy", fake_search)
    monkeypatch.setattr(taxonomy_service, "fetch_taxonomy_detail", fake_detail)

    rows = [
        {
            "key": "monkeypox virus",
            "organism": "Monkeypox virus",
            "taxid": "",
            "count": 100,
            "best_evalue": 0.0,
            "top_bitscore": 828.4,
            "organism_source": "stitle",
        }
    ]
    enriched, meta = enrich_taxonomy_with_lineage(rows, taxid_limit=20)
    assert meta["name_resolved"] == 1
    assert meta["looked_up"] == 1
    assert meta["failed"] == 0
    assert enriched[0]["taxid"] == "10244"
    assert enriched[0]["taxid_source"] == "name_lookup"
    assert enriched[0]["blast_name"] == "viruses"
    assert "Viruses" in enriched[0]["lineage"]


def test_enrich_taxonomy_blast_name_for_mammal_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The blast_name column should mirror NCBI's coarse groups
    (mammals, plants, fungi, …) derived from the lineage chain."""
    taxonomy_service.clear_taxonomy_cache()

    monkeypatch.setattr(
        taxonomy_service,
        "fetch_taxonomy_detail",
        lambda taxid: {
            "taxid": 9606,
            "scientific_name": "Homo sapiens",
            "lineage": "cellular organisms; Eukaryota; Metazoa; Chordata; Mammalia; Primates",
            "lineage_ex": [
                {"rank": "no rank", "taxid": 131567, "scientific_name": "cellular organisms"},
                {"rank": "superkingdom", "taxid": 2759, "scientific_name": "Eukaryota"},
                {"rank": "kingdom", "taxid": 33208, "scientific_name": "Metazoa"},
                {"rank": "phylum", "taxid": 7711, "scientific_name": "Chordata"},
                {"rank": "class", "taxid": 40674, "scientific_name": "Mammalia"},
                {"rank": "order", "taxid": 9443, "scientific_name": "Primates"},
            ],
        },
    )

    rows = [
        {
            "key": "9606",
            "organism": "Homo sapiens",
            "taxid": "9606",
            "count": 5,
            "best_evalue": 1e-30,
            "top_bitscore": 500.0,
            "organism_source": "sscinames",
        }
    ]
    enriched, meta = enrich_taxonomy_with_lineage(rows, taxid_limit=20)
    assert meta["looked_up"] == 1
    assert enriched[0]["blast_name"] == "mammals"


def test_enrich_taxonomy_skips_unresolvable_organism(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `search_taxonomy` returns no candidates the row is left
    untouched (no taxid filled, no lineage)."""
    taxonomy_service.clear_taxonomy_cache()

    monkeypatch.setattr(
        taxonomy_service,
        "search_taxonomy",
        lambda query, *, limit=10: {
            "query": query,
            "count": 0,
            "source": "test",
            "cached": False,
            "results": [],
        },
    )

    detail_called: list[int] = []
    monkeypatch.setattr(
        taxonomy_service,
        "fetch_taxonomy_detail",
        lambda taxid: detail_called.append(taxid) or {},
    )

    rows = [
        {
            "key": "nonsense organism",
            "organism": "Nonsense organism",
            "taxid": "",
            "count": 3,
            "best_evalue": None,
            "top_bitscore": None,
            "organism_source": "stitle",
        }
    ]
    enriched, meta = enrich_taxonomy_with_lineage(rows, taxid_limit=20)
    assert meta["name_resolved"] == 0
    assert meta["looked_up"] == 0
    assert enriched[0]["taxid"] == ""
    assert "lineage" not in enriched[0]
    assert detail_called == []

