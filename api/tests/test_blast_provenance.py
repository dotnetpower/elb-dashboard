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


def test_build_blast_provenance_falls_back_to_external_db_name() -> None:
    """External-origin BLAST jobs (synced from the sibling OpenAPI plane) often
    carry the database name only inside payload['external']['db_name'] -- the
    top-level 'db' field stays empty because the local /submit pipeline never
    ran. The provenance builder MUST surface that fallback so the citation
    Methods paragraph names the database instead of saying 'selected database'
    (issue #11)."""
    payload = {
        "job_id": "job-ext-1",
        "program": "blastn",
        # No top-level 'db' (mimics external sync row).
        "external": {
            "source": "elastic-blast-azure",
            "db_name": "16S_ribosomal_RNA",
        },
        "query_fasta": ">q1\nATGC\n",
    }
    provenance = build_blast_provenance(job_id="job-ext-1", payload=payload)
    assert provenance["database"]["name"] == "16S_ribosomal_RNA"


def test_build_blast_provenance_prefers_external_db_when_top_level_blank() -> None:
    """Same as above, but the sibling sent 'db' (not 'db_name'). The fallback
    chain must try both keys in order."""
    payload = {
        "job_id": "job-ext-2",
        "program": "blastn",
        "external": {"db": "nt"},
    }
    provenance = build_blast_provenance(job_id="job-ext-2", payload=payload)
    assert provenance["database"]["name"] == "nt"
