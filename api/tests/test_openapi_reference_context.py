"""Tests for the OpenAPI Web BLAST reference-context resolver overlay.

Responsibility: Verify that paired NCBI XML1/XML2 evidence produces the exact six-field context
and that ambiguous or mismatched evidence fails closed.
Edit boundaries: Use checked-in fixtures and injected fetchers only; never call NCBI or Azure.
Key entry points: `test_reference_fixtures_reproduce_context`, `test_resolver_caches_same_evidence`.
Risky contracts: A test must never infer missing filtered counts or silently normalize an unknown
database-length representation.
Validation: `uv run pytest -q api/tests/test_openapi_reference_context.py`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "web_blast_parity"


def _load_module() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "dev"
        / "openapi-overlays"
        / "reference_context.py"
    )
    spec = importlib.util.spec_from_file_location("openapi_reference_context", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _payloads() -> dict[str, object]:
    return json.loads((FIXTURES / "reference_payloads.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("gene_id", ["f3l", "rrna_18s", "rdrp_orf1ab"])
def test_reference_fixtures_reproduce_context(gene_id: str) -> None:
    module = _load_module()
    payloads = _payloads()
    case = payloads["genes"][gene_id]
    snapshot = payloads["core_nt_snapshot"]
    query_fasta = (FIXTURES / case["fasta_path"]).read_text(encoding="utf-8")

    resolved = module.derive_reference_context(
        rid=case["ncbi_rid"],
        query_fasta=query_fasta,
        xml1_payload=(FIXTURES / case["reference_xml_path"]).read_bytes(),
        xml2_payload=(FIXTURES / case["reference_xml2_path"]).read_bytes(),
        active_total_letters=snapshot["number_of_letters"],
        active_total_sequences=snapshot["number_of_sequences"],
        active_source_version=snapshot["local_active_generation"],
        taxid=case["exclusion_taxid"],
        is_inclusive=False,
    )

    expected = case["dashboard_request"]["web_blast_statistical_context"]
    assert resolved["web_blast_statistical_context"] == expected
    assert resolved["query_effective_search_spaces"] == [expected["effective_search_space"]]
    assert resolved["query_length"] == case["query_length"]
    assert resolved["expected_filter"] == {
        "taxid": case["exclusion_taxid"],
        "is_inclusive": False,
        "verified_from_result": False,
    }
    assert resolved["evidence"]["query_content_verified"] is False
    assert resolved["evidence"]["submission_options_verified"] is False
    assert resolved["evidence"]["active_source_version_verified"] is False
    assert len(resolved["evidence"]["xml1_sha256"]) == 64
    assert len(resolved["evidence"]["xml2_sha256"]) == 64
    assert "do not prove" in resolved["warnings"][0]


def test_reference_context_rejects_query_length_mismatch() -> None:
    module = _load_module()
    payloads = _payloads()
    case = payloads["genes"]["f3l"]
    snapshot = payloads["core_nt_snapshot"]

    with pytest.raises(module.ReferenceContextError, match="query length"):
        module.derive_reference_context(
            rid=case["ncbi_rid"],
            query_fasta=">different\nACGT\n",
            xml1_payload=(FIXTURES / case["reference_xml_path"]).read_bytes(),
            xml2_payload=(FIXTURES / case["reference_xml2_path"]).read_bytes(),
            active_total_letters=snapshot["number_of_letters"],
            active_total_sequences=snapshot["number_of_sequences"],
            active_source_version=snapshot["local_active_generation"],
        )


def test_reference_context_rejects_unknown_database_length_encoding() -> None:
    module = _load_module()
    with pytest.raises(module.ReferenceContextError, match="database length"):
        module.derive_reference_context(
            rid="ABCDEFGH1234",
            query_fasta=">q1\n" + "A" * 100,
            xml1_payload=(
                b"<BlastOutput><BlastOutput_program>blastn</BlastOutput_program>"
                b"<BlastOutput_db>core_nt</BlastOutput_db>"
                b"<BlastOutput_query-ID>q1</BlastOutput_query-ID>"
                b"<BlastOutput_query-len>100</BlastOutput_query-len>"
                b"<BlastOutput_iterations><Iteration><Iteration_stat><Statistics>"
                b"<Statistics_db-num>10</Statistics_db-num>"
                b"<Statistics_db-len>123</Statistics_db-len>"
                b"</Statistics></Iteration_stat></Iteration></BlastOutput_iterations>"
                b"</BlastOutput>"
            ),
            xml2_payload=(
                b"<BlastOutput2><report><results><search><stat><Statistics>"
                b"<db-num>10</db-num><db-len>123</db-len><hsp-len>1</hsp-len>"
                b"<eff-space>8811</eff-space><kappa>1</kappa><lambda>1</lambda>"
                b"<entropy>1</entropy></Statistics></stat></search></results></report>"
                b"</BlastOutput2>"
            ),
            active_total_letters=1000,
            active_total_sequences=20,
            active_source_version="generation-1",
        )


def test_reference_context_accepts_zero_length_adjustment() -> None:
    module = _load_module()
    resolved = module.derive_reference_context(
        rid="ABCDEFGH",
        query_fasta=">q1\nACGT\n",
        xml1_payload=(
            b"<BlastOutput><BlastOutput_program>blastn</BlastOutput_program>"
            b"<BlastOutput_db>core_nt</BlastOutput_db>"
            b"<BlastOutput_query-ID>q1</BlastOutput_query-ID>"
            b"<BlastOutput_query-len>4</BlastOutput_query-len>"
            b"<BlastOutput_iterations><Iteration><Iteration_stat><Statistics>"
            b"<Statistics_db-num>10</Statistics_db-num>"
            b"<Statistics_db-len>1000</Statistics_db-len>"
            b"</Statistics></Iteration_stat></Iteration></BlastOutput_iterations>"
            b"</BlastOutput>"
        ),
        xml2_payload=(
            b"<BlastOutput2><report><results><search><stat><Statistics>"
            b"<db-num>10</db-num><db-len>1000</db-len><hsp-len>0</hsp-len>"
            b"<eff-space>4000</eff-space></Statistics></stat></search></results>"
            b"</report></BlastOutput2>"
        ),
        active_total_letters=1000,
        active_total_sequences=10,
        active_source_version="generation-1",
    )

    assert resolved["web_blast_statistical_context"]["length_adjustment"] == 0


def test_reference_context_rejects_xml_entities() -> None:
    module = _load_module()
    payload = (
        b'<!DOCTYPE BlastOutput [<!ENTITY injected "blocked">]>'
        b"<BlastOutput><BlastOutput_program>&injected;</BlastOutput_program></BlastOutput>"
    )

    with pytest.raises(module.ReferenceContextError, match="forbidden XML features"):
        module._parse_xml(payload, source="NCBI XML1")


def test_resolver_fails_fast_when_fetch_gate_is_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()

    class BusyLock:
        def acquire(self, *, timeout: float) -> bool:
            assert timeout == module._FETCH_LOCK_WAIT_SECONDS
            return False

        def release(self) -> None:
            raise AssertionError("an unacquired fetch gate must not be released")

    monkeypatch.setattr(module, "_FETCH_LOCK", BusyLock())
    with pytest.raises(module.ReferenceContextUnavailable, match="resolver is busy"):
        module.resolve_reference_context(
            rid="ABCDEFGH",
            query_fasta=">q1\nACGT\n",
            active_total_letters=1000,
            active_total_sequences=10,
            active_source_version="generation-1",
            fetch_payload=lambda _rid, _format: b"",
        )


def test_resolver_caches_same_evidence() -> None:
    module = _load_module()
    payloads = _payloads()
    case = payloads["genes"]["f3l"]
    snapshot = payloads["core_nt_snapshot"]
    query_fasta = (FIXTURES / case["fasta_path"]).read_text(encoding="utf-8")
    responses = {
        "XML": (FIXTURES / case["reference_xml_path"]).read_bytes(),
        "XML2": (FIXTURES / case["reference_xml2_path"]).read_bytes(),
    }
    calls: list[str] = []

    def fetch(_rid: str, format_type: str) -> bytes:
        calls.append(format_type)
        return responses[format_type]

    kwargs = {
        "rid": case["ncbi_rid"],
        "query_fasta": query_fasta,
        "active_total_letters": snapshot["number_of_letters"],
        "active_total_sequences": snapshot["number_of_sequences"],
        "active_source_version": snapshot["local_active_generation"],
        "taxid": case["exclusion_taxid"],
        "is_inclusive": False,
        "fetch_payload": fetch,
    }
    first = module.resolve_reference_context(**kwargs)
    expected_search_space = first["web_blast_statistical_context"][
        "effective_search_space"
    ]
    first["web_blast_statistical_context"]["effective_search_space"] = -1
    second = module.resolve_reference_context(**kwargs)

    assert second["web_blast_statistical_context"][
        "effective_search_space"
    ] == expected_search_space
    assert calls == ["XML", "XML2"]
