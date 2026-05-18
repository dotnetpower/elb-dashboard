"""Tests for `api.services.blast_results_parser`.

Covers BLAST `-outfmt 6` (no header) and `-outfmt 7` (with `# Fields:`
comment lines), numeric coercion, custom column orderings, and the
aggregate statistics shape expected by `web/src/pages/BlastAnalytics.tsx`.
"""

from __future__ import annotations

from api.services.blast_results_parser import (
    EXPORT_DEFAULT_COLUMNS,
    aggregate_blast_hits,
    parse_blast_result_content,
    parse_blast_tabular,
    parse_blast_xml,
)

_OUTFMT_6_SAMPLE = "\n".join(
    [
        # query, subject, pident, length, mismatch, gapopen, qstart, qend,
        # sstart, send, evalue, bitscore
        "query1\tNC_001\t99.50\t150\t1\t0\t1\t150\t100\t249\t1e-50\t289",
        "query1\tNC_002\t95.00\t148\t7\t1\t1\t148\t200\t347\t2e-40\t250",
        "query2\tNC_001\t100.00\t120\t0\t0\t1\t120\t1\t120\t3e-60\t320",
        "query3\tNC_003\t85.50\t90\t13\t0\t1\t90\t10\t99\t0.01\t75",
    ]
)


_OUTFMT_7_WITH_HEADER = """\
# BLASTN 2.17.0+
# Query: query_alpha
# Database: nt
# Fields: query acc.ver, subject acc.ver, % identity, alignment length, mismatches, gap opens, q. start, q. end, s. start, s. end, evalue, bit score, % positives, query length, subject length
# 3 hits found
query_alpha\tMN908947\t99.10\t200\t1\t0\t1\t200\t10\t209\t1e-100\t450\t100\t220\t29903
query_alpha\tMT121215\t98.50\t200\t3\t0\t1\t200\t1\t200\t1e-90\t420\t99\t220\t29903
query_beta\tFJ441177\t87.20\t250\t30\t2\t1\t250\t10\t258\t1e-30\t180\t90\t260\t30000
"""  # noqa: E501  -- BLAST -outfmt 7 fixture; line length is intrinsic to the format


_OUTFMT_5_XML = """<?xml version="1.0"?>
<BlastOutput>
    <BlastOutput_iterations>
        <Iteration>
            <Iteration_query-ID>query_alpha</Iteration_query-ID>
            <Iteration_query-len>462</Iteration_query-len>
            <Iteration_hits>
                <Hit>
                    <Hit_id>gi|1|gb|ABC123.1|</Hit_id>
                    <Hit_accession>ABC123</Hit_accession>
                    <Hit_def>Example subject sequence</Hit_def>
                    <Hit_len>1200</Hit_len>
                    <Hit_hsps><Hsp>
                        <Hsp_identity>460</Hsp_identity><Hsp_positive>461</Hsp_positive>
                        <Hsp_align-len>462</Hsp_align-len><Hsp_gaps>1</Hsp_gaps>
                        <Hsp_query-from>1</Hsp_query-from><Hsp_query-to>462</Hsp_query-to>
                        <Hsp_hit-from>10</Hsp_hit-from><Hsp_hit-to>471</Hsp_hit-to>
                        <Hsp_evalue>1e-100</Hsp_evalue><Hsp_bit-score>828.419</Hsp_bit-score>
                        <Hsp_score>448</Hsp_score><Hsp_qseq>ACGT</Hsp_qseq>
                        <Hsp_hseq>ACGA</Hsp_hseq><Hsp_midline>||| </Hsp_midline>
                    </Hsp></Hit_hsps>
                </Hit>
            </Iteration_hits>
        </Iteration>
    </BlastOutput_iterations>
</BlastOutput>
"""


def test_parse_outfmt6_default_columns() -> None:
    hits = parse_blast_tabular(_OUTFMT_6_SAMPLE)
    assert len(hits) == 4
    first = hits[0]
    assert first["qseqid"] == "query1"
    assert first["sseqid"] == "NC_001"
    assert first["pident"] == 99.50
    assert first["length"] == 150
    assert first["mismatch"] == 1
    assert first["gapopen"] == 0
    assert first["qstart"] == 1
    assert first["qend"] == 150
    assert first["sstart"] == 100
    assert first["send"] == 249
    assert first["evalue"] == 1e-50
    assert first["bitscore"] == 289.0


def test_parse_outfmt7_uses_field_header() -> None:
    hits = parse_blast_tabular(_OUTFMT_7_WITH_HEADER)
    assert len(hits) == 3
    first = hits[0]
    # Columns from the # Fields: header should be honoured.
    assert first["qseqid"] == "query_alpha"
    assert first["sseqid"] == "MN908947"
    assert first["ppos"] == 100.0
    assert first["qlen"] == 220
    assert first["slen"] == 29903
    # And numeric coercion still works.
    assert isinstance(first["pident"], float)
    assert isinstance(first["length"], int)
    assert isinstance(first["evalue"], float)


def test_parse_outfmt5_xml_to_canonical_hit_rows() -> None:
    hits = parse_blast_xml(_OUTFMT_5_XML)
    assert len(hits) == 1
    hit = hits[0]
    assert hit["qseqid"] == "query_alpha"
    assert hit["sseqid"] == "ABC123.1"
    assert hit["stitle"] == "Example subject sequence"
    assert hit["qlen"] == 462
    assert hit["slen"] == 1200
    assert hit["pident"] == 99.567
    assert hit["ppos"] == 99.784
    assert hit["length"] == 462
    assert hit["mismatch"] == 1
    assert hit["gapopen"] == 1
    assert hit["gaps"] == 1
    assert hit["qstart"] == 1
    assert hit["qend"] == 462
    assert hit["sstart"] == 10
    assert hit["send"] == 471
    assert hit["evalue"] == 1e-100
    assert hit["bitscore"] == 828.419
    assert hit["score"] == 448
    assert hit["qseq"] == "ACGT"
    assert hit["sseq"] == "ACGA"
    assert hit["midline"] == "|||"


def test_parse_result_content_detects_xml() -> None:
    hits = parse_blast_result_content(_OUTFMT_5_XML)
    assert len(hits) == 1
    assert hits[0]["sseqid"] == "ABC123.1"


def test_parse_result_content_detects_bom_prefixed_xml() -> None:
    hits = parse_blast_result_content("\ufeff\n" + _OUTFMT_5_XML)
    assert len(hits) == 1
    assert hits[0]["qseqid"] == "query_alpha"


def test_parse_outfmt5_xml_tolerates_namespaces() -> None:
    namespaced = _OUTFMT_5_XML.replace("<BlastOutput>", '<BlastOutput xmlns="urn:test">')
    hits = parse_blast_xml(namespaced)
    assert len(hits) == 1
    assert hits[0]["sseqid"] == "ABC123.1"


def test_parse_skips_blank_and_comment_lines() -> None:
    content = "\n".join(
        [
            "# comment",
            "",
            "   ",
            "# Fields: query acc.ver, subject acc.ver, % identity, alignment length, mismatches, gap opens, q. start, q. end, s. start, s. end, evalue, bit score",  # noqa: E501  -- BLAST -outfmt 7 fixture
            "qX\tsX\t99.0\t100\t1\t0\t1\t100\t1\t100\t1e-50\t200",
        ]
    )
    hits = parse_blast_tabular(content)
    assert len(hits) == 1
    assert hits[0]["qseqid"] == "qX"


def test_parse_tolerates_unparseable_numeric_fields() -> None:
    # Length is non-integer "?" — should fall back to string instead of crashing.
    content = "qX\tsX\t99.0\t?\t1\t0\t1\t100\t1\t100\t1e-50\t200"
    hits = parse_blast_tabular(content)
    assert len(hits) == 1
    assert hits[0]["length"] == "?"


def test_parse_skips_short_lines() -> None:
    # 5 columns is not enough for the 12-column default — line is dropped.
    content = "qX\tsX\t99.0\t100\t1"
    hits = parse_blast_tabular(content)
    assert hits == []


def test_aggregate_shape_for_outfmt6() -> None:
    hits = parse_blast_tabular(_OUTFMT_6_SAMPLE)
    stats = aggregate_blast_hits(hits)
    assert stats["total_hits"] == 4
    assert stats["unique_queries"] == 3
    assert stats["unique_subjects"] == 3
    assert stats["max_bitscore"] == 320.0
    assert stats["min_evalue"] == 3e-60
    assert isinstance(stats["evalue_distribution"], dict)
    assert isinstance(stats["identity_distribution"], dict)
    assert isinstance(stats["top_subjects"], list)
    assert {item["id"] for item in stats["top_subjects"]} == {"NC_001", "NC_002", "NC_003"}
    # NC_001 has 2 hits, NC_002 / NC_003 have 1 each → it must be on top.
    assert stats["top_subjects"][0]["id"] == "NC_001"
    assert stats["top_subjects"][0]["count"] == 2


def test_top_hit_per_query_picks_lowest_evalue() -> None:
    hits = parse_blast_tabular(_OUTFMT_6_SAMPLE)
    stats = aggregate_blast_hits(hits)
    top_per_q = {row["qseqid"]: row for row in stats["top_hit_per_query"]}
    assert top_per_q["query1"]["sseqid"] == "NC_001"  # 1e-50 beats 2e-40
    assert top_per_q["query1"]["evalue"] == 1e-50
    assert top_per_q["query2"]["sseqid"] == "NC_001"
    assert top_per_q["query3"]["sseqid"] == "NC_003"


def test_aggregate_handles_empty_input() -> None:
    stats = aggregate_blast_hits([])
    assert stats["total_hits"] == 0
    assert stats["unique_queries"] == 0
    assert stats["unique_subjects"] == 0
    assert stats["avg_identity"] is None
    assert stats["avg_bitscore"] is None
    assert stats["min_evalue"] is None
    assert stats["top_subjects"] == []
    assert stats["top_hit_per_query"] == []


def test_evalue_distribution_bin_assignment() -> None:
    # One hit per documented bin so the totals are easy to assert.
    rows = [
        ("qZ", "sZ", "99.0", "100", "0", "0", "1", "100", "1", "100", "0", "100"),  # 0
        ("qZ", "sZ", "99.0", "100", "0", "0", "1", "100", "1", "100", "1e-150", "100"),
        ("qZ", "sZ", "99.0", "100", "0", "0", "1", "100", "1", "100", "1e-70", "100"),
        ("qZ", "sZ", "99.0", "100", "0", "0", "1", "100", "1", "100", "1e-20", "100"),
        ("qZ", "sZ", "99.0", "100", "0", "0", "1", "100", "1", "100", "1e-7", "100"),
        ("qZ", "sZ", "99.0", "100", "0", "0", "1", "100", "1", "100", "0.001", "100"),
        ("qZ", "sZ", "99.0", "100", "0", "0", "1", "100", "1", "100", "0.5", "100"),
        ("qZ", "sZ", "99.0", "100", "0", "0", "1", "100", "1", "100", "5", "100"),
        ("qZ", "sZ", "99.0", "100", "0", "0", "1", "100", "1", "100", "100", "100"),
    ]
    content = "\n".join("\t".join(row) for row in rows)
    hits = parse_blast_tabular(content)
    stats = aggregate_blast_hits(hits)
    dist = stats["evalue_distribution"]
    assert dist["0"] == 1
    assert dist["1e-200..1e-100"] == 1
    assert dist["1e-100..1e-50"] == 1
    assert dist["1e-50..1e-10"] == 1
    assert dist["1e-10..1e-5"] == 1
    assert dist["1e-5..0.01"] == 1
    assert dist["0.01..1"] == 1
    assert dist["1..10"] == 1
    assert dist[">10"] == 1


def test_export_columns_cover_outfmt6() -> None:
    assert "qseqid" in EXPORT_DEFAULT_COLUMNS
    assert "bitscore" in EXPORT_DEFAULT_COLUMNS
    assert len(EXPORT_DEFAULT_COLUMNS) == 12
