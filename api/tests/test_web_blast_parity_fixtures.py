"""Web BLAST → ElasticBLAST request parity for reference diagnostic genes.

Responsibility: Prove that the NCBI Web BLAST form payload captured for each
reference gene maps 1:1 into the BLAST+ command-line options this dashboard's
`generate_config()` emits in the elastic-blast INI it submits to Azure.

Edit boundaries: Tests only. The mapping policy itself lives in
`api/services/blast/config.py` and `api/tests/fixtures/web_blast_parity/README.md`;
this module is the contract guard. Do not relax assertions just to keep a test
green — the whole point is to fail loudly when a Web BLAST form field stops
matching the BLAST+ invocation we send.

Key entry points: `test_dashboard_request_matches_ncbi_form`,
`test_generate_config_emits_expected_blast_options`,
`test_fasta_length_matches_payload`, `test_blockers_are_explicitly_tracked`.

Risky contracts: Reads fixture JSON from disk; treats it as authoritative. Run
the suite to validate that the JSON, the FASTA, and the BLAST+ flag list stay
consistent whenever any parameter is added or renamed.

Validation: `uv run pytest -q api/tests/test_web_blast_parity_fixtures.py`.
"""

from __future__ import annotations

import configparser
import io
import json
from pathlib import Path
from typing import Any

import pytest
from api.services.blast.config import generate_config

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "web_blast_parity"
PAYLOADS_PATH = FIXTURES_DIR / "reference_payloads.json"


def _load_payloads() -> dict[str, Any]:
    return json.loads(PAYLOADS_PATH.read_text(encoding="utf-8"))


def _parse_ini(content: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read_file(io.StringIO(content))
    return parser


def _gene_ids() -> list[str]:
    return list(_load_payloads()["genes"].keys())


def _build_submit_params(gene_payload: dict[str, Any]) -> dict[str, Any]:
    """Compose the flat params dict that `generate_config()` consumes.

    The fixture only carries the BLAST-option-relevant fields (those that the
    NCBI Web BLAST form actually controls). Everything else — Azure region,
    storage account, AKS cluster name — is dashboard infrastructure plumbing
    irrelevant to Web BLAST parity, so it is filled in with a fixed stub here.
    """
    dashboard = gene_payload["dashboard_request"]
    db_name = dashboard["database_name"]
    return {
        # ---- Web BLAST-controlled fields (the contract under test) ----
        "program": dashboard["program"],
        "db": f"https://elbstg01.blob.core.windows.net/blast-db/{db_name}/{db_name}",
        "db_name": db_name,
        "evalue": dashboard["evalue"],
        "word_size": dashboard["word_size"],
        "max_target_seqs": dashboard["max_target_seqs"],
        "low_complexity_filter": dashboard["low_complexity_filter"],
        "taxid": dashboard["taxid"],
        "is_inclusive": dashboard["is_inclusive"],
        "outfmt": dashboard["outfmt"],
        # ---- Azure infrastructure plumbing (stubbed; not under test here) ----
        "region": "koreacentral",
        "resource_group": "rg-elb",
        "storage_account": "elbstg01",
        "aks_cluster_name": "elb-cluster",
        "machine_type": "Standard_E16s_v5",
        "num_nodes": 1,
        "query_blob_url": "https://elbstg01.blob.core.windows.net/queries/q.fa",
        "results_url": "https://elbstg01.blob.core.windows.net/results/job-1",
        "job_id": "parity-job",
    }


# ---------------------------------------------------------------------------
# Fixture/payload integrity
# ---------------------------------------------------------------------------


def test_reference_payloads_have_required_genes() -> None:
    """All three reference fixtures must always be present.

    Adding a new reference gene is welcome; removing one is a regression.
    """
    payloads = _load_payloads()
    assert "f3l" in payloads["genes"], "F3L fixture must remain present"
    assert "rrna_18s" in payloads["genes"], "18S rRNA fixture must remain present"
    assert "rdrp_orf1ab" in payloads["genes"], "RdRp/ORF1ab fixture must remain present"


@pytest.mark.parametrize("gene_id", _gene_ids())
def test_fasta_length_matches_payload(gene_id: str) -> None:
    """Reject a fixture mismatch between the captured FASTA and `query_length`.

    Catches accidental re-wraps / truncations of the reference sequence at
    review time before the contract test ever runs.
    """
    payload = _load_payloads()["genes"][gene_id]
    fasta_path = FIXTURES_DIR / payload["fasta_path"]
    text = fasta_path.read_text(encoding="utf-8")
    seq = "".join(line.strip() for line in text.splitlines() if not line.startswith(">"))
    assert len(seq) == payload["query_length"], (
        f"{gene_id}: FASTA is {len(seq)} bp, payload claims {payload['query_length']} bp"
    )


# ---------------------------------------------------------------------------
# NCBI Web BLAST form → dashboard request mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gene_id", _gene_ids())
def test_dashboard_request_matches_ncbi_form(gene_id: str) -> None:
    """The structured dashboard request must literally encode the NCBI form.

    This is the human-readable mapping table from
    `api/tests/fixtures/web_blast_parity/README.md` enforced as code.
    """
    payload = _load_payloads()["genes"][gene_id]
    form = payload["ncbi_form"]
    dashboard = payload["dashboard_request"]

    assert form["PROGRAM"] == dashboard["program"], "PROGRAM → program"
    assert form["DATABASE"] == dashboard["database_name"], "DATABASE → database_name"
    assert int(form["HITLIST_SIZE"]) == dashboard["max_target_seqs"], (
        "HITLIST_SIZE → max_target_seqs"
    )
    assert float(form["EXPECT"]) == dashboard["evalue"], "EXPECT → evalue"
    assert int(form["WORD_SIZE"]) == dashboard["word_size"], "WORD_SIZE → word_size"
    # FORMAT_TYPE=XML → outfmt=5; this is required so the dashboard's BLAST
    # output is in the same canonical XML format as the NCBI Web BLAST
    # reference (and so the comparator can parse both with the same code).
    assert form["FORMAT_TYPE"] == "XML", (
        f"{gene_id}: NCBI reference must request FORMAT_TYPE=XML for canonical comparison"
    )
    assert dashboard["outfmt"] == 5, (
        f"{gene_id}: FORMAT_TYPE=XML must map to outfmt=5 (tabular outfmt=6 cannot "
        "be compared against the captured XML reference)"
    )
    # FILTER=L → low-complexity masking on; absent/"F" → masking off.
    assert form["FILTER"] == "L"
    assert dashboard["low_complexity_filter"] is True, (
        "FILTER=L must map to low_complexity_filter=true"
    )
    # MEGABLAST=on with PROGRAM=blastn → modern BLAST+ defaults `-task megablast`,
    # which we reach by submitting program=blastn (no explicit -task flag).
    assert form["MEGABLAST"] == "on"
    assert dashboard["program"] == "blastn"
    # ENTREZ_QUERY=NOT txid<N>[ORGN] → negative_taxids <N>.
    expected_taxid = payload["exclusion_taxid"]
    assert f"NOT txid{expected_taxid}[ORGN]" == form["ENTREZ_QUERY"]
    assert dashboard["taxid"] == expected_taxid
    assert dashboard["is_inclusive"] is False, (
        "ENTREZ_QUERY=NOT ... must map to is_inclusive=false (negative taxid filter)"
    )


# ---------------------------------------------------------------------------
# Dashboard request → BLAST+ flags emitted by generate_config()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gene_id", _gene_ids())
def test_generate_config_emits_expected_blast_options(gene_id: str) -> None:
    """Every BLAST+ flag the NCBI form implies must appear in `[blast].options`.

    `generate_config()` is the single source of truth for translating a
    structured dashboard submit into an elastic-blast.ini, so asserting the
    flag list here gates the whole BLAST/ElasticBLAST execution path.
    """
    payload = _load_payloads()["genes"][gene_id]
    params = _build_submit_params(payload)

    ini = generate_config(params)
    cfg = _parse_ini(ini)

    assert cfg.get("blast", "program") == payload["dashboard_request"]["program"]
    options = cfg.get("blast", "options")

    for expected_flag in payload["expected_blast_options"]:
        assert expected_flag in options, (
            f"{gene_id}: expected BLAST+ flag `{expected_flag}` missing from "
            f"[blast].options=`{options}`"
        )


@pytest.mark.parametrize("gene_id", _gene_ids())
def test_generated_ini_does_not_leak_inclusive_taxid_filter(gene_id: str) -> None:
    """`-taxids <N>` must never appear when the NCBI form requested exclusion.

    Inclusion (`-taxids`) and exclusion (`-negative_taxids`) are opposite
    filters; emitting both, or the wrong one, silently inverts the biological
    meaning of the search.
    """
    payload = _load_payloads()["genes"][gene_id]
    params = _build_submit_params(payload)

    options = _parse_ini(generate_config(params)).get("blast", "options")
    inclusive_flag = f"-taxids {payload['exclusion_taxid']}"
    assert inclusive_flag not in options, (
        f"{gene_id}: exclusive filter must not emit `{inclusive_flag}`; "
        f"observed options: {options}"
    )


# ---------------------------------------------------------------------------
# Outstanding blockers from issue #8 must stay visible
# ---------------------------------------------------------------------------


def test_blockers_are_explicitly_tracked() -> None:
    """Blocker bookkeeping must stay coherent with the captured fixture set.

    The fixture file carries an explicit `blockers` dict. When a gene is
    promoted from "blocked" to "captured" we expect the corresponding entry
    to be removed *and* the gene to have a reference XML on disk. This test
    guards both directions: a stale blocker pointing at a captured gene, and
    a missing blocker for an absent gene, both fail loudly.
    """
    payloads = _load_payloads()
    assert "blockers" in payloads, "blockers section must remain present"
    genes = payloads["genes"]
    for gene_id, blocker in payloads["blockers"].items():
        assert gene_id not in genes or "reference_xml_path" not in genes[gene_id], (
            f"{gene_id} appears in blockers but already has a reference XML "
            f"captured; remove the stale blocker entry"
        )
        assert blocker.get("status"), (
            f"{gene_id}: blocker entry must carry a non-empty status string"
        )
    for gene_id, payload in genes.items():
        xml_path = payload.get("reference_xml_path")
        if not xml_path:
            assert gene_id in payloads["blockers"], (
                f"{gene_id} has no reference_xml_path but is not tracked as a blocker"
            )
            continue
        assert (FIXTURES_DIR / xml_path).exists(), (
            f"{gene_id}: reference XML {xml_path} missing on disk"
        )
