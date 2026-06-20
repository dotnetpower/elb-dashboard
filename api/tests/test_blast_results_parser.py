"""Tests for `api.services.blast.results_parser`.

Responsibility: Tests for `api.services.blast.results_parser`
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_parse_outfmt6_default_columns`, `test_parse_outfmt7_uses_field_header`,
`test_parse_outfmt5_xml_to_canonical_hit_rows`, `test_parse_result_content_detects_xml`,
`test_parse_result_content_detects_bom_prefixed_xml`,
`test_parse_outfmt5_xml_tolerates_namespaces`,
`test_parse_outfmt5_xml_extracts_query_and_subject_frame`,
`test_parse_outfmt5_xml_drops_zero_valued_frame`,
`test_parse_outfmt7_with_frame_header_extracts_qframe_and_sframe`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_blast_results_parser.py`.
"""

from __future__ import annotations

from api.services.blast.results_parser import (
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


def test_parse_outfmt7_maps_taxonomy_and_coverage_columns() -> None:
    # Real blastn 2.17.0 `# Fields:` header for the dashboard's
    # `-outfmt 7 std staxids sscinames stitle qcovs` taxonomy + description
    # toggle. Without the qcovs label alias the parser would name the column
    # `%_query_coverage_per_subject` and the UI's HSP Cover (hit.qcovs) would
    # render blank even though the value is present.
    content = (
        "# BLASTN 2.17.0+\n"
        "# Query: NC_003310.1:c48509-48048 Monkeypox virus, complete genome\n"
        "# Database: core_nt_shard_01\n"
        "# Fields: query acc.ver, subject acc.ver, % identity, alignment length, "
        "mismatches, gap opens, q. start, q. end, s. start, s. end, evalue, "
        "bit score, subject tax ids, subject sci names, subject title, "
        "% query coverage per subject\n"
        "# 1 hits found\n"
        "NC_003310.1:c48509-48048\tPQ305795.1\t100.000\t462\t0\t0\t1\t462\t"
        "47222\t46761\t0.0\t854\t10244\tMonkeypox virus\t"
        "Monkeypox virus isolate 45_DRC, complete genome\t97\n"
    )
    hits = parse_blast_tabular(content)
    assert len(hits) == 1
    hit = hits[0]
    assert hit["staxids"] == "10244"
    assert hit["sscinames"] == "Monkeypox virus"
    # stitle keeps its embedded comma/spaces (tab-delimited, one field).
    assert hit["stitle"] == "Monkeypox virus isolate 45_DRC, complete genome"
    # qcovs is mapped from "% query coverage per subject" and coerced to float.
    assert hit["qcovs"] == 97.0
    assert isinstance(hit["qcovs"], float)


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


# --------------------------------------------------------------------------- #
# Reading-frame extraction for translated BLAST programs (blastx / tblastn /
# tblastx). NCBI Web BLAST surfaces a "Frame" column on the Descriptions /
# Alignments tabs for translated programs; without parser support that column
# would be silently empty even when the underlying BLAST output carries it.
# --------------------------------------------------------------------------- #

_OUTFMT_5_TRANSLATED_XML = """<?xml version="1.0"?>
<BlastOutput>
    <BlastOutput_iterations>
        <Iteration>
            <Iteration_query-ID>query_translated</Iteration_query-ID>
            <Iteration_query-len>900</Iteration_query-len>
            <Iteration_hits>
                <Hit>
                    <Hit_id>gi|2|gb|XYZ987.1|</Hit_id>
                    <Hit_accession>XYZ987</Hit_accession>
                    <Hit_def>Translated subject sequence</Hit_def>
                    <Hit_len>300</Hit_len>
                    <Hit_hsps><Hsp>
                        <Hsp_identity>290</Hsp_identity><Hsp_positive>295</Hsp_positive>
                        <Hsp_align-len>300</Hsp_align-len><Hsp_gaps>0</Hsp_gaps>
                        <Hsp_query-from>1</Hsp_query-from><Hsp_query-to>900</Hsp_query-to>
                        <Hsp_hit-from>1</Hsp_hit-from><Hsp_hit-to>300</Hsp_hit-to>
                        <Hsp_evalue>1e-150</Hsp_evalue><Hsp_bit-score>540</Hsp_bit-score>
                        <Hsp_score>290</Hsp_score>
                        <Hsp_query-frame>2</Hsp_query-frame>
                        <Hsp_hit-frame>-1</Hsp_hit-frame>
                        <Hsp_qseq>MTEXAMPLE</Hsp_qseq>
                        <Hsp_hseq>MTEXAMPLE</Hsp_hseq><Hsp_midline>|||||||||</Hsp_midline>
                    </Hsp></Hit_hsps>
                </Hit>
            </Iteration_hits>
        </Iteration>
    </BlastOutput_iterations>
</BlastOutput>
"""


def test_parse_outfmt5_xml_extracts_query_and_subject_frame() -> None:
    """Translated BLAST programs emit Hsp_query-frame / Hsp_hit-frame; the
    canonical hit row must surface them as qframe / sframe so the UI can
    render NCBI's "Frame" column.
    """
    hits = parse_blast_xml(_OUTFMT_5_TRANSLATED_XML)
    assert len(hits) == 1
    hit = hits[0]
    assert hit["qframe"] == 2
    assert hit["sframe"] == -1


def test_parse_outfmt5_xml_drops_zero_valued_frame() -> None:
    """Nucleotide / protein-only programs emit ``0`` for the frame fields;
    surfacing ``Frame: 0`` would mislead researchers, so the parser drops
    zero values entirely.
    """
    zero_frame_xml = _OUTFMT_5_TRANSLATED_XML.replace(
        "<Hsp_query-frame>2</Hsp_query-frame>",
        "<Hsp_query-frame>0</Hsp_query-frame>",
    ).replace(
        "<Hsp_hit-frame>-1</Hsp_hit-frame>",
        "<Hsp_hit-frame>0</Hsp_hit-frame>",
    )
    hits = parse_blast_xml(zero_frame_xml)
    assert len(hits) == 1
    hit = hits[0]
    assert "qframe" not in hit
    assert "sframe" not in hit


def test_parse_outfmt7_with_frame_header_extracts_qframe_and_sframe() -> None:
    """The tabular ``# Fields:`` header carries label aliases like
    ``query frame`` / ``subject frame``; the parser must coerce them to
    int and into the canonical qframe / sframe column names.
    """
    content = "\n".join(
        [
            "# BLASTX 2.17.0+",
            "# Query: q1",
            "# Database: nr",
            (
                "# Fields: query acc.ver, subject acc.ver, % identity, "
                "alignment length, mismatches, gap opens, q. start, q. end, "
                "s. start, s. end, evalue, bit score, query frame, "
                "subject frame"
            ),
            "# 1 hits found",
            "q1\tABC\t99.5\t100\t1\t0\t1\t300\t1\t100\t1e-50\t180\t2\t-1",
        ]
    )
    hits = parse_blast_tabular(content)
    assert len(hits) == 1
    hit = hits[0]
    assert hit["qframe"] == 2
    assert hit["sframe"] == -1


def test_parse_outfmt7_with_spaced_taxid_header_maps_staxids_and_sscinames() -> None:
    """blastn (BLAST+ 2.17.0) writes the taxonomy columns as ``subject tax
    ids`` / ``subject sci names`` (with spaces). The parser must map both to
    the canonical ``staxids`` / ``sscinames`` names so the dashboard's
    Scientific Name and Taxonomy views populate, instead of falling back to
    ``subject_tax_ids`` and dropping the value.
    """
    content = "\n".join(
        [
            "# BLASTN 2.17.0+",
            "# Query: q1",
            "# Database: core_nt",
            (
                "# Fields: query acc.ver, subject acc.ver, % identity, "
                "alignment length, mismatches, gap opens, q. start, q. end, "
                "s. start, s. end, evalue, bit score, subject tax ids, "
                "subject sci names"
            ),
            "# 1 hits found",
            "q1\tPQ221797.1\t100.000\t462\t0\t0\t1\t462\t1\t462\t0.0\t828\t10244\tMonkeypox virus",
        ]
    )
    hits = parse_blast_tabular(content)
    assert len(hits) == 1
    hit = hits[0]
    assert hit["staxids"] == "10244"
    assert hit["sscinames"] == "Monkeypox virus"
    assert "subject_tax_ids" not in hit
