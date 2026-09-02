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
`test_reference_xml_self_equivalence`,
`test_query_source_accession_excluded`, `test_reference_exclusion_blockers_are_explicit`,
`test_dashboard_xml_parser_agrees_with_reference_parser`,
`test_candidate_xml_matches_reference_when_provided`.

Risky contracts: candidate XML comparison is optional; when
`ELB_PARITY_CANDIDATE_DIR` is unset the layer skips cleanly. Once supplied,
every candidate must be same-snapshot `exact_equivalent`; drift diagnostics do
not satisfy the issue-closing gate.

Validation: `uv run pytest -q api/tests/test_web_blast_parity_xml.py`.
"""

from __future__ import annotations

import gzip
import json
import os
from dataclasses import replace
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
    "ppos",
    "length",
    "mismatch",
    "gapopen",
    "gaps",
    "qstart",
    "qend",
    "sstart",
    "send",
    "evalue",
    "bitscore",
    "score",
    "qlen",
    "slen",
    "stitle",
    "qseq",
    "sseq",
    "midline",
    "qframe",
    "sframe",
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
    assert summary.evalue_threshold == pytest.approx(payload["dashboard_request"]["evalue"])
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
    assert report.exact_equivalent is True
    assert report.comparison_mode == "strict_exact"
    assert report.exact_findings == []
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


def test_cross_snapshot_diagnostic_does_not_claim_exact_equivalence() -> None:
    payload = _load_payloads()["genes"]["f3l"]
    reference = parse_summary(FIXTURES_DIR / payload["reference_xml_path"])
    first_hit = reference.hits[0]
    first_hsp = first_hit.hsps[0]
    changed_hsp = replace(first_hsp, raw_score=first_hsp.raw_score - 1)
    changed_hit = replace(first_hit, hsps=(changed_hsp, *first_hit.hsps[1:]))
    candidate = replace(
        reference,
        db_len=reference.db_len + 1,
        hits=(changed_hit, *reference.hits[1:-1]),
    )

    report = compare_summaries(reference, candidate)

    assert report.comparison_mode == "drift_tolerant_containment"
    assert report.equivalent is True
    assert report.drift_compatible is True
    assert report.exact_equivalent is False
    assert report.findings == []
    assert report.hsp_drift
    assert any("database snapshot mismatch" in item for item in report.exact_findings)


def test_cross_snapshot_diagnostic_rejects_empty_candidate() -> None:
    payload = _load_payloads()["genes"]["f3l"]
    reference = parse_summary(FIXTURES_DIR / payload["reference_xml_path"])
    candidate = replace(reference, db_num=reference.db_num + 1, hits=())

    report = compare_summaries(reference, candidate)

    assert report.equivalent is False
    assert report.exact_equivalent is False
    assert report.drift_compatible is False
    assert "candidate contains no hits while reference contains hits" in report.findings


def test_database_path_is_representation_only_but_query_def_is_strict() -> None:
    payload = _load_payloads()["genes"]["f3l"]
    reference = parse_summary(FIXTURES_DIR / payload["reference_xml_path"])
    path_variant = replace(
        reference,
        database="https://account.blob.core.windows.net/blast-db/core_nt/core_nt",
    )
    query_variant = replace(reference, query_def=f"{reference.query_def} changed")

    assert compare_summaries(reference, path_variant).exact_equivalent is True
    query_report = compare_summaries(reference, query_variant)
    assert query_report.exact_equivalent is False
    assert any("query definition mismatch" in item for item in query_report.exact_findings)


def test_parse_summary_rejects_multiple_query_iterations(tmp_path: Path) -> None:
    payload = _load_payloads()["genes"]["f3l"]
    source = _read_xml_text(FIXTURES_DIR / payload["reference_xml_path"])
    start = source.index("<Iteration>")
    end = source.index("</Iteration>", start) + len("</Iteration>")
    candidate = source.replace(
        "</BlastOutput_iterations>",
        f"{source[start:end]}</BlastOutput_iterations>",
        1,
    )
    path = tmp_path / "multiple-queries.xml"
    path.write_text(candidate, encoding="utf-8")

    with pytest.raises(ValueError, match="expected exactly one query iteration, found 2"):
        parse_summary(path)


def test_duplicate_accession_keys_fail_exact_comparison() -> None:
    payload = _load_payloads()["genes"]["f3l"]
    reference = parse_summary(FIXTURES_DIR / payload["reference_xml_path"])
    duplicate = replace(reference.hits[1], subject_id=reference.hits[0].subject_id)
    candidate = replace(reference, hits=(reference.hits[0], duplicate, *reference.hits[2:]))

    report = compare_summaries(reference, candidate)

    assert report.exact_equivalent is False
    assert any("duplicate accession keys" in item for item in report.exact_findings)


@pytest.mark.parametrize(
    "field_name",
    (
        "bit_score",
        "raw_score",
        "evalue",
        "query_from",
        "query_to",
        "hit_from",
        "hit_to",
        "query_frame",
        "hit_frame",
        "identity",
        "positive",
        "gaps",
        "align_len",
        "qseq",
        "hseq",
        "midline",
    ),
)
def test_same_snapshot_all_hsp_fields_are_strictly_compared(field_name: str) -> None:
    payload = _load_payloads()["genes"]["f3l"]
    reference = parse_summary(FIXTURES_DIR / payload["reference_xml_path"])
    first_hit = reference.hits[0]
    first_hsp = first_hit.hsps[0]
    original = getattr(first_hsp, field_name)
    changed = f"{original}X" if isinstance(original, str) else original + 1
    changed_hsp = replace(first_hsp, **{field_name: changed})
    changed_hit = replace(first_hit, hsps=(changed_hsp, *first_hit.hsps[1:]))
    candidate = replace(reference, hits=(changed_hit, *reference.hits[1:]))

    report = compare_summaries(reference, candidate)

    assert report.comparison_mode == "strict_exact"
    assert report.equivalent is False
    assert report.exact_equivalent is False
    assert report.hsp_drift[0]["hsp_index"] == 1
    assert field_name in report.hsp_drift[0]
    assert any("subject/HSP records drifted" in item for item in report.findings)


@pytest.mark.parametrize("gene_id", _captured_genes())
def test_parse_summary_retains_every_hsp(gene_id: str) -> None:
    payload = _load_payloads()["genes"][gene_id]
    xml_path = FIXTURES_DIR / payload["reference_xml_path"]
    summary = parse_summary(xml_path)

    assert sum(len(hit.hsps) for hit in summary.hits) == _read_xml_text(xml_path).count("<Hsp>")


def test_same_snapshot_subject_and_hsp_count_drift_fail_exactly() -> None:
    payload = _load_payloads()["genes"]["f3l"]
    reference = parse_summary(FIXTURES_DIR / payload["reference_xml_path"])
    first_hit = reference.hits[0]
    changed_hit = replace(first_hit, rank=first_hit.rank + 1, hsps=())
    candidate = replace(reference, hits=(changed_hit, *reference.hits[1:]))

    report = compare_summaries(reference, candidate)

    assert report.exact_equivalent is False
    subject_drift = report.hsp_drift[0]["subject"]
    assert "rank" in subject_drift
    assert subject_drift["hsp_count"] == {
        "reference": len(first_hit.hsps),
        "candidate": 0,
    }


@pytest.mark.parametrize(
    "field_name",
    ("eff_space", "hsp_len", "kappa", "lambda_value", "entropy", "parameters"),
)
def test_same_snapshot_statistics_and_parameters_are_strict(field_name: str) -> None:
    payload = _load_payloads()["genes"]["f3l"]
    reference = parse_summary(FIXTURES_DIR / payload["reference_xml_path"])
    original = getattr(reference, field_name)
    if field_name == "parameters":
        changed = (*original, ("Parameters_test-only", "different"))
    else:
        changed = original + 1
    candidate = replace(reference, **{field_name: changed})

    report = compare_summaries(reference, candidate)

    assert report.exact_equivalent is False
    assert report.findings


# ---------------------------------------------------------------------------
# Taxonomic exclusion verification (always runs)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gene_id", _captured_genes())
def test_query_source_accession_excluded(gene_id: str) -> None:
    """The query's source accession must not appear as a canonical subject.

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
    assert violations == [], f"{gene_id}: query source accession leaked into hits: {violations}"


def test_reference_exclusion_blockers_are_explicit() -> None:
    payloads = _load_payloads()["genes"]
    blockers = {
        gene_id: payload["exclusion_validation_blocker"]
        for gene_id, payload in payloads.items()
        if payload.get("exclusion_validation_blocker")
    }

    assert set(blockers) == {"rdrp_orf1ab"}
    blocker = blockers["rdrp_orf1ab"]
    assert blocker["code"] == "grouped_defline_contains_excluded_descendant"
    assert blocker["subject_accession"] == "MN996528.1"
    assert blocker["subject_taxid"] == 2_697_049
    assert blocker["excluded_ancestor_taxid"] == 3_418_604


# ---------------------------------------------------------------------------
# Canonical-field guard (always runs) — wires the comparator to the
# dashboard's own XML parser so the UI/API/export cannot silently drop a
# field while these tests still pass.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gene_id", _captured_genes())
def test_dashboard_xml_parser_agrees_with_reference_parser(gene_id: str) -> None:
    """The dashboard parser must preserve every canonical field on every HSP.

    `parse_blast_xml` (results_parser.py) is what feeds the dashboard's
    result-list UI, API responses, and CSV exports. If any row disagrees with
    the comparator's complete reference view, the dashboard is misrepresenting
    NCBI output regardless of how clean the INI ↔ flag mapping looks.
    """
    payload = _load_payloads()["genes"][gene_id]
    xml_path = FIXTURES_DIR / payload["reference_xml_path"]
    summary = parse_summary(xml_path)

    dashboard_rows = parse_blast_xml(_read_xml_text(xml_path))
    assert dashboard_rows, f"{gene_id}: dashboard parser produced no rows"

    expected = [(hit, hsp) for hit in summary.hits for hsp in hit.hsps]
    assert len(dashboard_rows) == len(expected)
    for row_index, (row, (hit, hsp)) in enumerate(
        zip(dashboard_rows, expected, strict=True),
        start=1,
    ):
        for field in _CANONICAL_HIT_FIELDS:
            assert field in row, (
                f"{gene_id}: row {row_index} missing canonical field `{field}`; "
                "UI/API/CSV will drop it"
            )
        expected_values = {
            "qseqid": summary.query_id,
            "pident": round(hsp.identity * 100.0 / hsp.align_len, 3),
            "ppos": round(hsp.positive * 100.0 / hsp.align_len, 3),
            "length": hsp.align_len,
            "mismatch": max(0, hsp.align_len - hsp.identity - hsp.gaps),
            "gapopen": hsp.gaps,
            "gaps": hsp.gaps,
            "qstart": hsp.query_from,
            "qend": hsp.query_to,
            "sstart": hsp.hit_from,
            "send": hsp.hit_to,
            "evalue": hsp.evalue,
            "bitscore": hsp.bit_score,
            "score": hsp.raw_score,
            "qlen": summary.query_len,
            "slen": hit.hit_len,
            "stitle": hit.hit_def,
            "qseq": hsp.qseq,
            "sseq": hsp.hseq,
            "midline": hsp.midline,
            "qframe": hsp.query_frame,
            "sframe": hsp.hit_frame,
        }
        assert row["sseqid"] == hit.subject_id
        for field, expected_value in expected_values.items():
            assert row[field] == expected_value, (
                f"{gene_id}: row {row_index} field {field} differs: "
                f"dashboard={row[field]!r} comparator={expected_value!r}"
            )


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
    """If the operator exports `ELB_PARITY_CANDIDATE_DIR`, exact parity must hold.

    The candidate directory layout mirrors the fixture: a per-gene XML named
    `<gene_id>.xml` (or `.xml.gz`). Once the directory is supplied, every
    captured gene is mandatory; a missing file fails instead of silently
    reducing acceptance coverage. The comparator must report
    `exact_equivalent=True`. A cross-snapshot candidate may still pass the
    separately reported containment diagnostic, but it cannot satisfy this
    issue-closing assertion.
    """
    candidate_dir = _candidate_dir()
    if candidate_dir is None:
        pytest.skip("ELB_PARITY_CANDIDATE_DIR not set; skipping candidate comparison")
    if not candidate_dir.is_dir():
        pytest.fail(f"ELB_PARITY_CANDIDATE_DIR={candidate_dir!s} is not a directory")

    candidate_path = None
    for suffix in (".xml", ".xml.gz"):
        candidate = candidate_dir / f"{gene_id}{suffix}"
        if candidate.exists():
            candidate_path = candidate
            break
    if candidate_path is None:
        pytest.fail(f"{gene_id}: no candidate XML in {candidate_dir!s}")

    payload = _load_payloads()["genes"][gene_id]
    reference = parse_summary(FIXTURES_DIR / payload["reference_xml_path"])
    candidate_summary = parse_summary(candidate_path)
    report = compare_summaries(reference, candidate_summary)
    assert report.exact_equivalent, (
        f"{gene_id}: candidate XML disagrees with reference XML.\n"
        f"  mode={report.comparison_mode}\n"
        f"  diagnostic_compatible={report.drift_compatible}\n"
        f"  drift={report.snapshot_drift}\n"
        f"  exact_findings={report.exact_findings}\n"
        f"  only_in_reference={report.rank_set_only_in_reference[:5]}\n"
        f"  only_in_candidate={report.rank_set_only_in_candidate[:5]}\n"
        f"  hsp_drift_sample={report.hsp_drift[:3]}"
    )


def test_supplied_candidate_directory_requires_every_gene(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ELB_PARITY_CANDIDATE_DIR", str(tmp_path))

    with pytest.raises(pytest.fail.Exception, match="f3l: no candidate XML"):
        test_candidate_xml_matches_reference_when_provided("f3l")
