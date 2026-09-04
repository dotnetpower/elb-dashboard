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
`test_candidate_xml_matches_reference_when_provided`,
`test_fresh_xml2_taxid_evidence_when_provided`.

Risky contracts: fresh reference and candidate XML comparison are optional;
when their environment variables are unset the layers skip cleanly. Once
supplied, all three references/candidates are mandatory, every candidate must
be same-snapshot `exact_equivalent`, and XML2 taxonomy evidence must contain no
missing or forbidden descendant taxids. Drift diagnostics and title inference
do not satisfy the issue-closing gate.

Validation: `uv run pytest -q api/tests/test_web_blast_parity_xml.py`.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
from api.services.blast.results_parser import parse_blast_xml
from api.services.blast.web_blast_parity import (
    WebBlastSummary,
    compare_summaries,
    parse_summary,
    parse_xml2_deflines,
    parse_xml2_statistics,
    verify_exclusion,
    verify_xml2_taxid_exclusion,
)
from defusedxml import ElementTree as ET

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


def _reference_dir() -> Path | None:
    raw = os.environ.get("ELB_PARITY_REFERENCE_DIR", "").strip()
    return Path(raw) if raw else None


def _reference_path(gene_id: str, payload: dict[str, Any]) -> Path:
    reference_dir = _reference_dir()
    if reference_dir is None:
        return FIXTURES_DIR / payload["reference_xml_path"]
    if not reference_dir.is_dir():
        pytest.fail(f"ELB_PARITY_REFERENCE_DIR={reference_dir!s} is not a directory")
    candidates = (
        reference_dir / f"{gene_id}.xml",
        reference_dir / f"{gene_id}.xml.gz",
        reference_dir / gene_id / "reference.xml",
        reference_dir / gene_id / "reference.xml.gz",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    pytest.fail(f"{gene_id}: no fresh reference XML in {reference_dir!s}")


def _reference_xml2_path(gene_id: str, payload: dict[str, Any]) -> Path | None:
    reference_dir = _reference_dir()
    if reference_dir is None:
        relative = payload.get("reference_xml2_path")
        return FIXTURES_DIR / relative if isinstance(relative, str) and relative else None
    candidates = (
        reference_dir / f"{gene_id}.xml2",
        reference_dir / f"{gene_id}.xml2.gz",
        reference_dir / gene_id / "reference.xml2.raw",
        reference_dir / gene_id / "reference.xml2.gz",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _parse_reference_summary(
    gene_id: str,
    payload: dict[str, Any],
) -> WebBlastSummary:
    summary = parse_summary(_reference_path(gene_id, payload))
    xml2_path = _reference_xml2_path(gene_id, payload)
    if xml2_path is None:
        return summary
    statistics = parse_xml2_statistics(xml2_path)
    return replace(
        summary,
        hsp_len=statistics.hsp_len if summary.hsp_len == 0 else summary.hsp_len,
        eff_space=statistics.eff_space if summary.eff_space == 0 else summary.eff_space,
    )


def _read_xml_text(xml_path: Path) -> str:
    """Read a captured reference XML transparently from `.xml` or `.xml.gz`."""
    if xml_path.suffix == ".gz":
        with gzip.open(xml_path, "rt", encoding="utf-8") as fh:
            return fh.read()
    return xml_path.read_text(encoding="utf-8")


def _read_evidence_bytes(path: Path) -> bytes:
    if path.suffix == ".gz":
        with gzip.open(path, "rb") as fh:
            return fh.read()
    return path.read_bytes()


# ---------------------------------------------------------------------------
# Structural / header guard (always runs)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gene_id", _captured_genes())
def test_reference_xml_parses_with_expected_header(gene_id: str) -> None:
    """The captured XML must declare blastn + BLASTN 2.x + core_nt + matching qlen."""
    payload = _load_payloads()["genes"][gene_id]
    summary = _parse_reference_summary(gene_id, payload)

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
    summary = _parse_reference_summary(gene_id, payload)
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
    summary = _parse_reference_summary(gene_id, payload)
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
    reference = _parse_reference_summary("f3l", payload)
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
    reference = _parse_reference_summary("f3l", payload)
    candidate = replace(reference, db_num=reference.db_num + 1, hits=())

    report = compare_summaries(reference, candidate)

    assert report.equivalent is False
    assert report.exact_equivalent is False
    assert report.drift_compatible is False
    assert "candidate contains no hits while reference contains hits" in report.findings


def test_wrapped_xml1_db_len_requires_authoritative_snapshot_proof() -> None:
    payload = _load_payloads()["genes"]["rdrp_orf1ab"]
    reference = _parse_reference_summary("rdrp_orf1ab", payload)
    full_filtered_db_len = reference.db_len + (176 * 2**32)
    assert full_filtered_db_len == 756_264_949_991
    candidate = replace(reference, db_len=full_filtered_db_len)

    unproven = compare_summaries(reference, candidate)
    proven = compare_summaries(
        reference,
        candidate,
        authoritative_snapshot_match=True,
    )

    assert unproven.exact_equivalent is False
    assert unproven.snapshot_drift is True
    assert unproven.db_len_representation_normalized is False
    assert proven.exact_equivalent is True
    assert proven.snapshot_drift is False
    assert proven.db_len_representation_normalized is True


def test_database_path_is_representation_only_but_query_def_is_strict() -> None:
    payload = _load_payloads()["genes"]["f3l"]
    reference = _parse_reference_summary("f3l", payload)
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
    source = _read_xml_text(_reference_path("f3l", payload))
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
    reference = _parse_reference_summary("f3l", payload)
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
    reference = _parse_reference_summary("f3l", payload)
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
    xml_path = _reference_path(gene_id, payload)
    summary = _parse_reference_summary(gene_id, payload)

    assert sum(len(hit.hsps) for hit in summary.hits) == _read_xml_text(xml_path).count("<Hsp>")


def test_same_snapshot_subject_and_hsp_count_drift_fail_exactly() -> None:
    payload = _load_payloads()["genes"]["f3l"]
    reference = _parse_reference_summary("f3l", payload)
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
    reference = _parse_reference_summary("f3l", payload)
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
    summary = _parse_reference_summary(gene_id, payload)

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


def test_reference_exclusion_evidence_is_explicit() -> None:
    payloads = _load_payloads()["genes"]
    evidence = {
        gene_id: payload["exclusion_validation_evidence"]
        for gene_id, payload in payloads.items()
        if payload.get("exclusion_validation_evidence")
    }

    assert set(evidence) == {"f3l", "rdrp_orf1ab"}
    for gene_id, item in evidence.items():
        assert item["status"] == "verified"
        assert item["evidence_format"] == "XML2"
        assert item["evidence_rid"] == payloads[gene_id]["ncbi_rid"]
        assert len(item["evidence_sha256"]) == 64
        assert len(item["reference_content_sha256"]) == 64
        assert len(item["taxonomy_response_sha256"]) == 64
        assert item["defline_count"] > 0
        assert item["missing_taxid_count"] == 0
        assert item["excluded_descendant_count"] == 0


@pytest.mark.parametrize(
    "gene_id",
    [
        gene_id
        for gene_id, payload in _load_payloads()["genes"].items()
        if payload.get("exclusion_validation_evidence")
    ],
)
def test_xml2_has_no_excluded_taxid_or_descendant(gene_id: str) -> None:
    """Verify every grouped defline against the pinned NCBI Taxonomy response."""
    payload = _load_payloads()["genes"][gene_id]
    evidence = payload["exclusion_validation_evidence"]
    xml2_path = FIXTURES_DIR / payload["reference_xml2_path"]
    taxonomy_path = FIXTURES_DIR / payload["reference_taxonomy_path"]
    xml2_bytes = _read_evidence_bytes(xml2_path)
    taxonomy_bytes = _read_evidence_bytes(taxonomy_path)

    assert hashlib.sha256(xml2_bytes).hexdigest() == evidence["reference_content_sha256"]
    assert hashlib.sha256(taxonomy_bytes).hexdigest() == evidence["taxonomy_response_sha256"]

    deflines = parse_xml2_deflines(xml2_path)
    result_taxids = {row.taxid for row in deflines if row.taxid is not None}
    taxonomy_root = ET.fromstring(taxonomy_bytes)
    checked_taxids: set[int] = set()
    descendant_taxids: set[int] = set()
    excluded_taxid = int(payload["exclusion_taxid"])
    for taxon in taxonomy_root.findall("./Taxon"):
        taxid = int(taxon.findtext("TaxId") or 0)
        checked_taxids.add(taxid)
        lineage = {
            int(node.findtext("TaxId") or 0)
            for node in taxon.findall("./LineageEx/Taxon")
        }
        if taxid == excluded_taxid or excluded_taxid in lineage:
            descendant_taxids.add(taxid)

    assert result_taxids <= checked_taxids
    assert len(deflines) == evidence["defline_count"]
    assert len(result_taxids) == evidence["unique_taxid_count"]
    assert sum(row.taxid is None for row in deflines) == evidence["missing_taxid_count"]
    assert len(descendant_taxids) == evidence["excluded_descendant_count"]
    assert verify_xml2_taxid_exclusion(
        deflines,
        forbidden_taxids={excluded_taxid, *descendant_taxids},
    ) == []


_XML2_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<BlastOutput2 xmlns="http://www.ncbi.nlm.nih.gov">
  <report><Report><results><Results><search><Search><hits>
    <Hit><num>3</num><description>
            <HitDescr>
                <id>gb|MN996528.1|</id><accession>MN996528</accession><taxid>2697049</taxid>
                <sciname>Severe acute respiratory syndrome coronavirus 2</sciname>
                <title>isolate WIV04</title>
            </HitDescr>
            <HitDescr>
                <id>gb|MT108784.1|</id><accession>MT108784</accession><taxid>32630</taxid>
                <sciname>synthetic construct</sciname><title>ORF1ab construct</title>
            </HitDescr>
            <HitDescr>
                <id>gb|NO_TAXID.1|</id><accession>NO_TAXID</accession>
                <sciname>unknown</sciname><title>missing taxonomy</title>
            </HitDescr>
    </description></Hit>
    </hits><stat><Statistics>
        <db-num>130155243</db-num><db-len>998069435926</db-len>
        <hsp-len>36</hsp-len><eff-space>421817959873974</eff-space>
        <kappa>0.46</kappa><lambda>1.28</lambda><entropy>0.85</entropy>
    </Statistics></stat></Search></search></Results></results></Report></report>
</BlastOutput2>
"""


def test_parse_xml2_deflines_retains_every_grouped_descriptor(tmp_path: Path) -> None:
    path = tmp_path / "reference.xml2"
    path.write_text(_XML2_SAMPLE, encoding="utf-8")

    deflines = parse_xml2_deflines(path)

    assert [(row.hit_rank, row.descriptor_index) for row in deflines] == [
        (3, 1),
        (3, 2),
        (3, 3),
    ]
    assert [row.accession for row in deflines] == ["MN996528", "MT108784", "NO_TAXID"]
    assert [row.taxid for row in deflines] == [2_697_049, 32_630, None]


def test_parse_xml2_deflines_reads_ncbi_zip_envelope(tmp_path: Path) -> None:
    archive_path = tmp_path / "reference.xml2.raw"
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(
            "RID.xml",
            '<?xml version="1.0"?><BlastXML2 xmlns="http://www.ncbi.nlm.nih.gov"/>',
        )
        archive.writestr("RID_1.xml", _XML2_SAMPLE)
    archive_path.write_bytes(buffer.getvalue())

    deflines = parse_xml2_deflines(archive_path)

    assert len(deflines) == 3
    assert deflines[0].identifier == "gb|MN996528.1|"


def test_parse_xml2_statistics_reads_authoritative_effective_space(tmp_path: Path) -> None:
    path = tmp_path / "reference.xml2"
    path.write_text(_XML2_SAMPLE, encoding="utf-8")

    statistics = parse_xml2_statistics(path)

    assert statistics.db_num == 130_155_243
    assert statistics.db_len == 998_069_435_926
    assert statistics.hsp_len == 36
    assert statistics.eff_space == 421_817_959_873_974
    assert statistics.kappa == 0.46
    assert statistics.lambda_value == 1.28
    assert statistics.entropy == 0.85


def test_xml2_taxid_exclusion_flags_descendants_and_missing_taxids(tmp_path: Path) -> None:
    path = tmp_path / "reference.xml2"
    path.write_text(_XML2_SAMPLE, encoding="utf-8")

    findings = verify_xml2_taxid_exclusion(
        parse_xml2_deflines(path),
        forbidden_taxids={3_418_604, 2_697_049},
    )

    assert [finding["code"] for finding in findings] == [
        "excluded_taxid_present",
        "missing_taxid",
    ]
    assert findings[0]["accession"] == "MN996528"
    assert findings[0]["taxid"] == 2_697_049


def test_fresh_xml2_taxid_evidence_when_provided() -> None:
    """A live reference directory must prove all grouped deflines taxid-safe."""
    reference_dir = _reference_dir()
    if reference_dir is None:
        pytest.skip("ELB_PARITY_REFERENCE_DIR not set; skipping fresh XML2 evidence")
    gene_dir = reference_dir / "rdrp_orf1ab"
    xml2_path = gene_dir / "reference.xml2.raw"
    evidence_path = gene_dir / "taxonomy-exclusion-evidence.json"
    if not xml2_path.exists():
        pytest.fail(f"rdrp_orf1ab: missing taxid-bearing XML2 at {xml2_path!s}")
    if not evidence_path.exists():
        pytest.fail(f"rdrp_orf1ab: missing taxonomy lineage evidence at {evidence_path!s}")

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    excluded_taxid = int(evidence["excluded_taxid"])
    descendant_taxids = {int(value) for value in evidence["descendant_taxids"]}
    findings = verify_xml2_taxid_exclusion(
        parse_xml2_deflines(xml2_path),
        forbidden_taxids={excluded_taxid, *descendant_taxids},
    )

    assert findings == [], (
        "rdrp_orf1ab: NCBI XML2 contains excluded taxid 3418604 or descendants; "
        f"findings={findings[:10]}"
    )


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
    xml_path = _reference_path(gene_id, payload)
    summary = _parse_reference_summary(gene_id, payload)

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
    reference = _parse_reference_summary(gene_id, payload)
    candidate_summary = parse_summary(candidate_path)
    snapshot = _load_payloads()["core_nt_snapshot"]
    report = compare_summaries(
        reference,
        candidate_summary,
        authoritative_snapshot_match=snapshot.get("identity_status") == "verified",
    )
    assert report.exact_equivalent, (
        f"{gene_id}: candidate XML disagrees with reference XML.\n"
        f"  mode={report.comparison_mode}\n"
        f"  diagnostic_compatible={report.drift_compatible}\n"
        f"  drift={report.snapshot_drift}\n"
        f"  db_len_representation_normalized="
        f"{report.db_len_representation_normalized}\n"
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
