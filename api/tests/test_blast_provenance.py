"""Tests for BLAST Provenance behavior.

Responsibility: Tests for BLAST Provenance behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_build_blast_provenance_captures_query_database_and_compatibility`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_blast_provenance.py`.
"""

from __future__ import annotations

from api.services.blast.provenance import build_blast_provenance
from api.services.blast.submit_payload import canonical_submit_snapshot, submit_contracts


def test_build_blast_provenance_captures_query_database_and_compatibility() -> None:
    payload = {
        "job_id": "job-1",
        "program": "blastn",
        "db": "core_nt",
        "query_fasta": ">q1\nATGCATGCATGC\n",
        "options": {"outfmt": 5, "word_size": 28, "dust": True},
    }
    payload["canonical_request"] = canonical_submit_snapshot(payload)
    payload.update(submit_contracts(payload))

    provenance = build_blast_provenance(job_id="job-1", payload=payload)

    assert provenance["job_id"] == "job-1"
    assert provenance["blast"]["program"] == "blastn"
    assert provenance["blast"]["version"] == "BLASTN 2.17.0+"
    assert provenance["database"]["name"] == "core_nt"
    assert provenance["database"]["search_space"] == 32_156_241_807_668
    assert provenance["query"]["kind"] == "inline_fasta"
    assert provenance["query"]["sha256"]
    assert provenance["compatibility"]["mode"] == "precise"
    assert provenance["artifact"]["path"] == "job-1/provenance.json"
