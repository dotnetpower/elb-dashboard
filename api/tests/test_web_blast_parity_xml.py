"""Web BLAST XML parity, taxonomy exclusion, and canonical-field guard.

Responsibility: Prove that for every reference gene captured in
`api/tests/fixtures/web_blast_parity/`, the canonical BLAST XML view used
by the dashboard is (a) self-consistent, (b) taxonomy-filter-correct, and
(c) numerically equivalent to a candidate XML produced by an actual
ElasticBLAST run when one is supplied via the `ELB_PARITY_CANDIDATE_DIR`
environment variable.

Edit boundaries: Tests only. The comparator + exclusion verifier live in
`api/services/blast/web_blast_parity.py`; the dashboard's reusable XML
parser lives in `api/services/blast/results_parser.py`. Do not duplicate
parsing or comparison logic here — extend those modules instead.

Key entry points: `test_reference_xml_parses_with_expected_header`,
`test_reference_xml_self_equivalence`, `test_query_source_accession_excluded`,
`test_dashboard_xml_parser_agrees_with_reference_parser`,
`test_candidate_xml_matches_reference_when_provided`.

Risky contracts: candidate XML comparison is optional; when
`ELB_PARITY_CANDIDATE_DIR` is unset the layer skips cleanly to keep CI
green while still providing the harness for operators that need it.

Validation: `uv run pytest -q api/tests/test_web_blast_parity_xml.py`.
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
from typing import Any

import pytest
from api.services.blast.results_parser import parse_blast_xml
from api.services.blast.web_blast_parity import (
    compare_summaries,
    parse_summary,
    verify_exclusion,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "web_blast_parity"
PAYLOADS_PATH = FIXTURES_DIR / "reference_payloads.json"

# Canonical fields every BLAST hit row in the dashboard UI / API / CSV
# export must keep carrying. These are the keys produced by
# `api/services/blast/results_parser.py::parse_blast_xml` and consumed by
# `api/services/blast/result_analytics.py`. If any of them is dropped here
# the dashboard will silently drop them in the response too.
_CANONICAL_HIT_FIELDS: tuple[str, ...] = (
    "qseqid",
    "sseqid",
    "pident",
    "length",
    "mismatch",
    "gapopen",
    "qstart",
    "qend",
    "sstart",
    "send",
    "evalue",
    "bitscore",
)


def _load_payloads() -> dict[str, Any]:
    return json.loads(PAYLOADS_PATH.read_text(encoding="utf-8"))


def _captured_genes() -> list[str]:
    payloads = _load_payloads()
    return [
        gene_id
        for gene_id, payload in payloads["genes"].items()
        if payload.get("reference_xml_path")
    ]


def _read_xml_text(xml_path: Path) -> str:
    """Read a captured reference XML transparently from `.xml` or `.xml.gz`."""
    if xml_path.suffix == ".gz":
        with gzip.open(xml_path, "rt", encoding="utf-8") as fh:
            return fh.read()
    return xml_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Structural / header guard (always runs)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gene_id", _captured_genes())
def test_reference_xml_parses_with_expected_header(gene_id: str) -> None:
    """The captured XML must declare blastn + BLASTN 2.x + core_nt + matching qlen."""
    payload = _load_payloads()["genes"][gene_id]
    xml_path = FIXTURES_DIR / payload["reference_xml_path"]
    summary = parse_summary(xml_path)

    assert summary.program == "blastn"
    assert summary.version.startswith("BLASTN 2."), (
        f"{gene_id}: unexpected BLASTN major version {summary.version!r}"
    )
    assert summary.database == "core_nt"
    assert summary.query_len == payload["query_length"], (
        f"{gene_id}: query_len in XML={summary.query_len} does not match "
        f"payload query_length={payload['query_length']}"
    )
    assert summary.evalue_threshold == pytest.approx(
        payload["dashboard_request"]["evalue"]
    )
    assert summary.filter_string.upper().startswith("L"), (
        f"{gene_id}: FILTER=L (low-complexity masking) must be active; "
        f"observed filter_string={summary.filter_string!r}"
    )
    assert summary.hits, f"{gene_id}: reference XML must contain at least one hit"


# ---------------------------------------------------------------------------
# Self-equivalence (always runs)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gene_id", _captured_genes())
def test_reference_xml_self_equivalence(gene_id: str) -> None:
    """Comparing the reference against itself must yield a clean equivalence.

    This is the smoke test for the comparator itself; if it ever returns a
    non-equivalent report against an identical XML, every other parity claim
    in this file is suspect.
    """
    payload = _load_payloads()["genes"][gene_id]
    xml_path = FIXTURES_DIR / payload["reference_xml_path"]
    summary = parse_summary(xml_path)
    report = compare_summaries(summary, summary)
    assert report.equivalent, f"{gene_id}: self-equivalence failed: {report.findings}"
    assert report.snapshot_drift is False
    assert report.rank_set_only_in_reference == []
    assert report.rank_set_only_in_candidate == []
    assert report.hsp_drift == []


@pytest.mark.parametrize("gene_id", _captured_genes())
def test_snapshot_drift_detail_is_populated(gene_id: str) -> None:
    """The comparator attaches a structured snapshot-drift verdict.

    When the candidate summary carries a database name, the report must expose
    a `snapshot_drift_detail` dict whose `status` is one of the known verdicts.
    This is the machine-readable counterpart to the `snapshot_drift` boolean.
    """
    payload = _load_payloads()["genes"][gene_id]
    xml_path = FIXTURES_DIR / payload["reference_xml_path"]
    summary = parse_summary(xml_path)
    report = compare_summaries(summary, summary)
    if not summary.database:
        assert report.snapshot_drift_detail is None
        return
    detail = report.snapshot_drift_detail
    assert isinstance(detail, dict)
    assert detail["status"] in {"match", "drift", "uncalibrated", "unknown"}
    assert "database" in detail
    assert "message" in detail


# ---------------------------------------------------------------------------
# Taxonomic exclusion verification (always runs)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gene_id", _captured_genes())
def test_query_source_accession_excluded(gene_id: str) -> None:
    """The query's own reference accession must not appear as a hit.

    `-negative_taxids` on our side and `ENTREZ_QUERY=NOT txid<N>[ORGN]` on
    NCBI's side must both eliminate the query's source organism from the
    result set. This is the strongest universal exclusion check (works
    regardless of where the excluded taxid sits in NCBI's tree).
    """
    payload = _load_payloads()["genes"][gene_id]
    xml_path = FIXTURES_DIR / payload["reference_xml_path"]
    summary = parse_summary(xml_path)

    # Map of gene → query source accession (the NCBI RefSeq the FASTA came
    # from). Hard-coded here because the fixture intentionally omits this
    # field — keeping it in test code prevents accidental "fix the fixture
    # to make the test pass" workarounds.
    query_sources = {
        "f3l": "NC_063383.1",
        "rrna_18s": "NC_004331.3",
        "rdrp_orf1ab": "NC_045512.2",
    }
    violations = verify_exclusion(
        summary,
        query_accession=query_sources[gene_id],
        excluded_markers=[],
    )
    assert violations == [], (
        f"{gene_id}: query source accession leaked into hits: {violations}"
    )


# ---------------------------------------------------------------------------
# Canonical-field guard (always runs) — wires the comparator to the
# dashboard's own XML parser so the UI/API/export cannot silently drop a
# field while these tests still pass.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gene_id", _captured_genes())
def test_dashboard_xml_parser_agrees_with_reference_parser(gene_id: str) -> None:
    """The dashboard's `parse_blast_xml` must agree on rank-1 with the comparator.

    `parse_blast_xml` (results_parser.py) is what feeds the dashboard's
    result-list UI, API responses, and CSV exports. If it disagrees with
    the comparator's view of the reference XML on the canonical fields, the
    dashboard is misrepresenting NCBI's output regardless of how clean the
    INI ↔ flag mapping looks.
    """
    payload = _load_payloads()["genes"][gene_id]
    xml_path = FIXTURES_DIR / payload["reference_xml_path"]
    summary = parse_summary(xml_path)

    dashboard_rows = parse_blast_xml(_read_xml_text(xml_path))
    assert dashboard_rows, f"{gene_id}: dashboard parser produced no rows"

    first_row = dashboard_rows[0]
    for field in _CANONICAL_HIT_FIELDS:
        assert field in first_row, (
            f"{gene_id}: canonical field `{field}` missing from "
            f"dashboard XML parser output; UI/API/CSV will drop it"
        )

    rank_one = summary.hits[0]
    # The dashboard parser emits one row per HSP, so the first row may belong
    # to a different HSP of the same rank-1 hit; the *subject accession* and
    # alignment length on that HSP must still match. Compare the accession
    # case-insensitively (BLAST tools sometimes upper-case Hit_id parts).
    assert first_row["sseqid"].split(".", 1)[0].upper() == rank_one.accession.upper(), (
        f"{gene_id}: rank-1 subject id mismatch: "
        f"dashboard={first_row['sseqid']!r} comparator={rank_one.accession!r}"
    )
    assert first_row["length"] == rank_one.align_len, (
        f"{gene_id}: rank-1 alignment length mismatch: "
        f"dashboard={first_row['length']} comparator={rank_one.align_len}"
    )
    assert first_row["bitscore"] == pytest.approx(rank_one.bit_score, rel=1e-6)
    if rank_one.evalue == 0.0:
        assert first_row["evalue"] == pytest.approx(0.0, abs=1e-300)
    else:
        assert first_row["evalue"] == pytest.approx(rank_one.evalue, rel=1e-6)


# ---------------------------------------------------------------------------
# Candidate-vs-reference comparison (skipped unless ELB_PARITY_CANDIDATE_DIR
# is set). This is the gate that an operator wires into a manual or CI run
# of an actual ElasticBLAST job for each reference gene.
# ---------------------------------------------------------------------------


def _candidate_dir() -> Path | None:
    raw = os.environ.get("ELB_PARITY_CANDIDATE_DIR", "").strip()
    if not raw:
        return None
    return Path(raw)


@pytest.mark.parametrize("gene_id", _captured_genes())
def test_candidate_xml_matches_reference_when_provided(gene_id: str) -> None:
    """If the operator exports `ELB_PARITY_CANDIDATE_DIR`, parity must hold.

    The candidate directory layout mirrors the fixture: a per-gene XML named
    `<gene_id>.xml` (or `.xml.gz`). When the file is missing for a particular
    gene the layer skips that gene only. When the file is present, the
    comparator must report `equivalent=True`; DB snapshot drift between the
    candidate and reference is allowed and only downgrades the strictness of
    the comparison (rank-set instead of per-HSP equality), it does NOT
    silence violations.
    """
    candidate_dir = _candidate_dir()
    if candidate_dir is None:
        pytest.skip("ELB_PARITY_CANDIDATE_DIR not set; skipping candidate comparison")
    if not candidate_dir.is_dir():
        pytest.fail(
            f"ELB_PARITY_CANDIDATE_DIR={candidate_dir!s} is not a directory"
        )

    candidate_path = None
    for suffix in (".xml", ".xml.gz"):
        candidate = candidate_dir / f"{gene_id}{suffix}"
        if candidate.exists():
            candidate_path = candidate
            break
    if candidate_path is None:
        pytest.skip(f"{gene_id}: no candidate XML in {candidate_dir!s}")

    payload = _load_payloads()["genes"][gene_id]
    reference = parse_summary(FIXTURES_DIR / payload["reference_xml_path"])
    candidate_summary = parse_summary(candidate_path)
    report = compare_summaries(reference, candidate_summary)
    assert report.equivalent, (
        f"{gene_id}: candidate XML disagrees with reference XML.\n"
        f"  drift={report.snapshot_drift}\n"
        f"  findings={report.findings}\n"
        f"  only_in_reference={report.rank_set_only_in_reference[:5]}\n"
        f"  only_in_candidate={report.rank_set_only_in_candidate[:5]}\n"
        f"  hsp_drift_sample={report.hsp_drift[:3]}"
    )
