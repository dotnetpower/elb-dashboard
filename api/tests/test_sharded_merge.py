"""Tests for Sharded Merge behavior.

Responsibility: Tests for Sharded Merge behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_blast_xml`, `test_merge_sharded_results_respects_top_n_and_report`,
`test_merge_sharded_results_reports_ties`,
`test_merge_sharded_results_reports_tie_cutoff_overflow`,
`test_merge_sharded_results_uses_tie_order_oracle`,
`test_merge_sharded_results_strict_oracle_excludes_non_oracle_hits`,
`test_db_order_oracle_reproduces_blast_reverse_oid_ties`,
`test_db_order_oracle_uses_raw_score_and_evalue_epsilon`,
`test_deterministic_tie_order_on_sorts_by_accession`,
`test_tabular_max_target_seqs_counts_subjects_and_preserves_hsps`,
`test_sequence_diversity_accepts_pool_above_legacy_limit`,
`test_sequence_diversity_bounds_candidate_pool_saturation_details`,
`test_sequence_diversity_large_pool_uses_disk_backed_bounded_memory`,
`test_sequence_diversity_publish_failure_is_atomic`,
`test_sequence_diversity_sigterm_cleans_temporary_artifacts`,
`test_large_sseq_rows_merge_under_bounded_memory`,
`test_large_db_order_oracle_streams_under_bounded_memory`,
`test_diversity_aware_cutoff_defaults_to_proportional_near_misses`,
`test_diversity_aware_cutoff_preserves_multiple_variants_at_5000`,
`test_xml_diversity_aware_cutoff_defaults_to_proportional_near_misses`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_sharded_merge.py`.
"""

from __future__ import annotations

import fcntl
import gzip
import json
import os
import resource
import signal
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = pytest.mark.subprocess

SCRIPT = Path(__file__).resolve().parents[2] / "terminal" / "merge-sharded-results.sh"


def _blast_xml(
    query_id: str,
    hits: list[tuple[str, str, float] | tuple[str, str, float, int]],
    *,
    db_len: int = 1000,
    db_num: int = 1,
    eff_space: int = 17928,
    hsp_len: int = 1,
) -> str:
    hit_xml = []
    for index, hit in enumerate(hits, start=1):
        subject, evalue, bitscore = hit[:3]
        raw_score = hit[3] if len(hit) == 4 else int(bitscore)
        hit_xml.append(
            f"""        <Hit>
          <Hit_num>{index}</Hit_num>
          <Hit_id>{subject}</Hit_id>
          <Hit_def>{subject}</Hit_def>
          <Hit_hsps>
            <Hsp>
              <Hsp_num>1</Hsp_num>
              <Hsp_bit-score>{bitscore}</Hsp_bit-score>
              <Hsp_score>{raw_score}</Hsp_score>
              <Hsp_evalue>{evalue}</Hsp_evalue>
            </Hsp>
          </Hit_hsps>
        </Hit>"""
        )
    return f"""<?xml version="1.0"?>
<BlastOutput>
  <BlastOutput_program>blastn</BlastOutput_program>
  <BlastOutput_version>BLASTN 2.17.0+</BlastOutput_version>
  <BlastOutput_db>child-db</BlastOutput_db>
  <BlastOutput_iterations>
    <Iteration>
      <Iteration_iter-num>1</Iteration_iter-num>
      <Iteration_query-ID>{query_id}</Iteration_query-ID>
      <Iteration_query-def>{query_id}</Iteration_query-def>
      <Iteration_query-len>10</Iteration_query-len>
      <Iteration_hits>
{chr(10).join(hit_xml)}
      </Iteration_hits>
            <Iteration_stat>
                <Statistics>
                    <Statistics_db-num>{db_num}</Statistics_db-num>
                    <Statistics_db-len>{db_len}</Statistics_db-len>
                    <Statistics_hsp-len>{hsp_len}</Statistics_hsp-len>
                    <Statistics_eff-space>{eff_space}</Statistics_eff-space>
                    <Statistics_kappa>0.46</Statistics_kappa>
                    <Statistics_lambda>1.28</Statistics_lambda>
                    <Statistics_entropy>0.85</Statistics_entropy>
                </Statistics>
            </Iteration_stat>
    </Iteration>
  </BlastOutput_iterations>
</BlastOutput>
"""


def test_merge_sharded_results_respects_top_n_and_report(tmp_path: Path) -> None:
    input_tsv = tmp_path / "hits.tsv"
    output_gz = tmp_path / "merged.out.gz"
    report_json = tmp_path / "merge-report.json"
    input_tsv.write_text(
        "\n".join(
            [
                "q1\ts2\t100\t20\t0\t0\t1\t20\t1\t20\t1e-20\t80",
                "q1\ts1\t100\t20\t0\t0\t1\t20\t1\t20\t1e-30\t90",
                "q1\ts3\t100\t20\t0\t0\t1\t20\t1\t20\t1e-10\t70",
                "q2\ts4\t100\t20\t0\t0\t1\t20\t1\t20\t1e-5\t60",
            ]
        )
        + "\n"
    )

    subprocess.run(  # noqa: S603 -- test executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_json),
            "2",
            "blastn",
            "-outfmt 6 -max_target_seqs 2",
        ],
        check=True,
    )

    with gzip.open(output_gz, "rt") as handle:
        rows = [line.strip() for line in handle if line.strip() and not line.startswith("#")]
    assert rows == [
        "q1\ts1\t100\t20\t0\t0\t1\t20\t1\t20\t1e-30\t90",
        "q1\ts2\t100\t20\t0\t0\t1\t20\t1\t20\t1e-20\t80",
        "q2\ts4\t100\t20\t0\t0\t1\t20\t1\t20\t1e-5\t60",
    ]

    report = json.loads(report_json.read_text())
    assert report["max_target_seqs"] == 2
    assert report["queries"] == 2
    assert report["total_input_hits"] == 4
    assert report["total_output_hits"] == 3


def test_merge_sharded_results_reports_ties(tmp_path: Path) -> None:
    input_tsv = tmp_path / "hits.tsv"
    output_gz = tmp_path / "merged.out.gz"
    report_json = tmp_path / "merge-report.json"
    input_tsv.write_text(
        "q1\ts1\t100\t20\t0\t0\t1\t20\t1\t20\t1e-30\t90\n"
        "q1\ts2\t100\t20\t0\t0\t1\t20\t1\t20\t1e-30\t90\n"
    )

    subprocess.run(  # noqa: S603 -- test executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_json),
            "2",
            "blastn",
            "-outfmt 6 -max_target_seqs 10",
        ],
        check=True,
    )

    report = json.loads(report_json.read_text())
    assert report["tie_break_count"] == 1
    assert report["tie_cutoff_overflow_count"] == 0
    assert report["warnings"]


def test_merge_sharded_results_reports_tie_cutoff_overflow(tmp_path: Path) -> None:
    input_tsv = tmp_path / "hits.tsv"
    output_gz = tmp_path / "merged.out.gz"
    report_json = tmp_path / "merge-report.json"
    input_tsv.write_text(
        "q1\ts1\t100\t20\t0\t0\t1\t20\t1\t20\t1e-30\t90\n"
        "q1\ts2\t100\t20\t0\t0\t1\t20\t1\t20\t1e-30\t90\n"
        "q1\ts3\t100\t20\t0\t0\t1\t20\t1\t20\t1e-30\t90\n"
    )

    subprocess.run(  # noqa: S603 -- test executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_json),
            "3",
            "blastn",
            "-outfmt 6 -max_target_seqs 2",
        ],
        check=True,
    )

    report = json.loads(report_json.read_text())
    assert report["tie_break_count"] == 2
    assert report["tie_cutoff_overflow_count"] == 1
    assert report["tie_cutoff_queries"] == [
        {
            "query_id": "q1",
            "evalue": 1e-30,
            "bitscore": 90.0,
            "tie_input_count": 3,
            "tie_selected_count": 2,
            "tie_overflow_count": 1,
        }
    ]
    assert any("max_target_seqs cutoff" in warning for warning in report["warnings"])


def test_merge_sharded_results_uses_tie_order_oracle(tmp_path: Path) -> None:
    input_tsv = tmp_path / "hits.tsv"
    output_gz = tmp_path / "merged.out.gz"
    report_json = tmp_path / "merge-report.json"
    oracle = tmp_path / "oracle.txt"
    input_tsv.write_text(
        "q1\ts1.1\t100\t20\t0\t0\t1\t20\t1\t20\t1e-30\t90\n"
        "q1\ts2.1\t100\t20\t0\t0\t1\t20\t1\t20\t1e-30\t90\n"
        "q1\ts3.1\t100\t20\t0\t0\t1\t20\t1\t20\t1e-30\t90\n"
    )
    oracle.write_text("s3\ns1\n")

    subprocess.run(  # noqa: S603 -- test executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_json),
            "3",
            "blastn",
            "-outfmt 6 -max_target_seqs 2",
        ],
        check=True,
        env={**os.environ, "ELB_TIE_ORDER_FILE": str(oracle)},
    )

    with gzip.open(output_gz, "rt") as handle:
        rows = [line.strip() for line in handle if line.strip() and not line.startswith("#")]
    assert rows == [
        "q1\ts3.1\t100\t20\t0\t0\t1\t20\t1\t20\t1e-30\t90",
        "q1\ts1.1\t100\t20\t0\t0\t1\t20\t1\t20\t1e-30\t90",
    ]

    report = json.loads(report_json.read_text())
    assert report["ranking_basis"] == "evalue_bitscore_oracle_ordinal"
    assert report["tie_order_oracle_accessions"] == 2
    assert report["tie_order_oracle_strict"] is False
    assert report["tie_cutoff_overflow_count"] == 1


def test_merge_sharded_results_strict_oracle_excludes_non_oracle_hits(tmp_path: Path) -> None:
    input_tsv = tmp_path / "hits.tsv"
    output_gz = tmp_path / "merged.out.gz"
    report_json = tmp_path / "merge-report.json"
    oracle = tmp_path / "oracle.txt"
    input_tsv.write_text(
        "q1\ts1.1\t100\t20\t0\t0\t1\t20\t1\t20\t1e-30\t90\n"
        "q1\tnon_oracle.1\t100\t20\t0\t0\t1\t20\t1\t20\t1e-40\t120\n"
        "q1\ts2.1\t100\t20\t0\t0\t1\t20\t1\t20\t1e-20\t80\n"
    )
    oracle.write_text("s2\ns1\n")

    subprocess.run(  # noqa: S603 -- test executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_json),
            "3",
            "blastn",
            "-outfmt 6 -max_target_seqs 10",
        ],
        check=True,
        env={**os.environ, "ELB_TIE_ORDER_FILE": str(oracle), "ELB_TIE_ORDER_STRICT": "1"},
    )

    with gzip.open(output_gz, "rt") as handle:
        rows = [line.strip() for line in handle if line.strip() and not line.startswith("#")]
    assert rows == [
        "q1\ts1.1\t100\t20\t0\t0\t1\t20\t1\t20\t1e-30\t90",
        "q1\ts2.1\t100\t20\t0\t0\t1\t20\t1\t20\t1e-20\t80",
    ]

    report = json.loads(report_json.read_text())
    assert report["tie_order_oracle_strict"] is True
    assert any("Strict tie-order oracle" in warning for warning in report["warnings"])


def test_db_order_oracle_reproduces_blast_reverse_oid_ties(tmp_path: Path) -> None:
    rows = [
        "q1\ts1\t1e-30\t90\t100",
        "q1\ts2\t1e-30\t90\t100",
        "q1\ts3\t1e-30\t90\t100",
    ]
    oracle = tmp_path / "db-order.txt"
    oracle.write_text("s1\ns2\ns3\n")

    out_rows, report = _run_tabular_merge(
        tmp_path,
        rows,
        num_shards="2",
        max_target_seqs=2,
        outfmt_spec="6 qseqid sseqid evalue bitscore score",
        env={
            "ELB_TIE_ORDER_FILE": str(oracle),
            "ELB_TIE_ORDER_SOURCE": "db_order",
        },
    )

    assert [row.split("\t")[1] for row in out_rows] == ["s3", "s2"]
    assert report["tie_order_oracle_source"] == "db_order"
    assert report["selection_equivalence"] == "full_db_hitlist_exact"
    assert report["diversity_reservation_mode"] == "db_order_exact"


def test_db_order_oracle_v2_maps_grouped_aliases_and_shard_oid_resets(
    tmp_path: Path,
) -> None:
    rows = [
        "q1\talias-a\t1e-30\t90\t100",
        "q1\ts2\t1e-30\t90\t100",
        "q1\ts3\t1e-30\t90\t100",
    ]
    oracle = tmp_path / "db-order-v2.txt"
    oracle.write_text("00\t0\tprimary-a\n00\t0\talias-a\n00\t1\ts2\n01\t0\ts3\n")

    out_rows, report = _run_tabular_merge(
        tmp_path,
        rows,
        num_shards="2",
        max_target_seqs=3,
        outfmt_spec="6 qseqid sseqid evalue bitscore score",
        env={
            "ELB_TIE_ORDER_FILE": str(oracle),
            "ELB_TIE_ORDER_SOURCE": "db_order",
        },
    )

    assert [row.split("\t")[1] for row in out_rows] == ["s3", "s2", "alias-a"]
    assert report["tie_order_oracle_accessions"] == 4
    assert report["selection_equivalence"] == "full_db_hitlist_exact"


def test_db_order_oracle_uses_raw_score_and_evalue_epsilon(tmp_path: Path) -> None:
    rows = [
        "q1\ts1\t0\t90\t100",
        "q1\ts2\t1e-200\t90\t101",
        "q1\ts3\t1e-50\t120\t130",
    ]
    oracle = tmp_path / "db-order.txt"
    oracle.write_text("s1\ns2\ns3\n")

    out_rows, report = _run_tabular_merge(
        tmp_path,
        rows,
        num_shards="2",
        max_target_seqs=2,
        outfmt_spec="6 qseqid sseqid evalue bitscore score",
        env={
            "ELB_TIE_ORDER_FILE": str(oracle),
            "ELB_TIE_ORDER_SOURCE": "db_order",
        },
    )

    assert [row.split("\t")[1] for row in out_rows] == ["s2", "s1"]
    assert report["resolved_columns"]["score"] == 4
    assert report["ranking_basis"] == "blast_evalue_raw_score_db_oid_desc"


def test_db_order_oracle_fails_when_candidate_is_unmapped(tmp_path: Path) -> None:
    input_tsv = tmp_path / "hits.tsv"
    output_gz = tmp_path / "merged.out.gz"
    report_json = tmp_path / "merge-report.json"
    oracle = tmp_path / "db-order.txt"
    input_tsv.write_text("q1\ts1\t1e-30\t90\t100\nq1\ts2\t1e-30\t90\t100\n")
    oracle.write_text("s1\n")

    proc = subprocess.run(  # noqa: S603 -- test executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_json),
            "2",
            "blastn",
            "-outfmt 6 qseqid sseqid evalue bitscore score -max_target_seqs 2",
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "ELB_TIE_ORDER_FILE": str(oracle),
            "ELB_TIE_ORDER_SOURCE": "db_order",
        },
    )

    assert proc.returncode != 0
    assert "DB-order oracle does not cover" in proc.stderr


def test_xml_db_order_oracle_uses_blast_comparator(tmp_path: Path) -> None:
    oracle = tmp_path / "db-order.txt"
    oracle.write_text("s1\ns2\ns3\n")

    subjects, report = _run_xml_merge(
        tmp_path,
        [
            [("s1", "0", 90.0, 100), ("s2", "1e-200", 90.0, 101)],
            [("s3", "1e-50", 120.0, 130)],
        ],
        max_target_seqs=2,
        env={
            "ELB_TIE_ORDER_FILE": str(oracle),
            "ELB_TIE_ORDER_SOURCE": "db_order",
        },
    )

    assert subjects == ["s2", "s1"]
    assert report["ranking_basis"] == "blast_evalue_raw_score_db_oid_desc"
    assert report["selection_equivalence"] == "full_db_hitlist_exact"


def test_merge_sharded_results_writes_valid_xml(tmp_path: Path) -> None:
    input_tsv = tmp_path / "all_hits.tsv"
    output_gz = tmp_path / "merged.out.gz"
    report_json = tmp_path / "merge-report.json"
    input_tsv.write_text("")
    for shard, hits in {
        "shard_00": [("subject_slow", "1e-10", 80.0), ("subject_best", "1e-30", 70.0)],
        "shard_01": [("subject_bit", "1e-20", 100.0)],
    }.items():
        shard_dir = tmp_path / shard
        shard_dir.mkdir()
        with gzip.open(shard_dir / "batch.out.gz", "wt") as handle:
            db_len = 1000 if shard == "shard_00" else 2000
            db_num = 1 if shard == "shard_00" else 2
            handle.write(_blast_xml("Query_1", hits, db_len=db_len, db_num=db_num))

    subprocess.run(  # noqa: S603 -- test executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_json),
            "2",
            "blastn",
            "-outfmt=5 -max_target_seqs=2",
        ],
        check=True,
    )

    with gzip.open(output_gz, "rt") as handle:
        xml_root = ET.parse(handle).getroot()  # noqa: S314 -- test fixture XML
    assert xml_root.tag == "BlastOutput"
    assert [node.text for node in xml_root.findall(".//Hit_id")] == [
        "subject_best",
        "subject_bit",
    ]
    statistics = xml_root.find(".//Iteration_stat/Statistics")
    assert statistics is not None
    assert statistics.findtext("Statistics_db-len") == "3000"
    assert statistics.findtext("Statistics_db-num") == "3"
    assert statistics.findtext("Statistics_eff-space") == "17928"
    assert statistics.findtext("Statistics_hsp-len") == "4"
    report = json.loads(report_json.read_text())
    assert report["outfmt"] == 5
    assert report["format"] == "blast_xml"


def test_xml_merge_applies_validated_web_blast_statistics_without_changing_hsps(
    tmp_path: Path,
) -> None:
    input_tsv = tmp_path / "hits.tsv"
    output_gz = tmp_path / "merged.out.gz"
    report_json = tmp_path / "merge-report.json"
    oracle = tmp_path / "db-order.txt"
    statistics_path = tmp_path / "web-blast-statistics.json"
    input_tsv.write_text("")
    oracle.write_text("s1\ns2\n")
    statistics = {
        "schema_version": 1,
        "query_id": "q1",
        "query_length": 10,
        "filtered_database_letters": 2900,
        "filtered_database_sequences": 2,
        "length_adjustment": 1,
        "effective_search_space": 26082,
        "scoring_search_space": 26100,
        "result_database_letters": 3000,
        "active_database_letters": 3000,
        "active_database_sequences": 3,
        "active_source_version": "generation-1",
    }
    statistics_path.write_text(json.dumps(statistics))
    for shard, hits, db_len, db_num in (
        ("shard_00", [("s1", "1.25e-20", 90.0)], 1000, 1),
        ("shard_01", [("s2", "2.5e-10", 80.0)], 2000, 2),
    ):
        shard_dir = tmp_path / shard
        shard_dir.mkdir()
        with gzip.open(shard_dir / "batch.out.gz", "wt") as handle:
            handle.write(
                _blast_xml(
                    "q1",
                    hits,
                    db_len=db_len,
                    db_num=db_num,
                    eff_space=26100,
                    hsp_len=1,
                )
            )

    subprocess.run(  # noqa: S603 -- test executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_json),
            "2",
            "blastn",
            "-outfmt 5 -max_target_seqs 2 -dbsize 2900 -searchsp 26100",
        ],
        check=True,
        env={
            **os.environ,
            "ELB_TIE_ORDER_FILE": str(oracle),
            "ELB_TIE_ORDER_SOURCE": "db_order",
            "ELB_WEB_BLAST_STATISTICS_FILE": str(statistics_path),
        },
    )

    with gzip.open(output_gz, "rt") as handle:
        root = ET.parse(handle).getroot()  # noqa: S314 -- test fixture XML
    stats = root.find(".//Iteration_stat/Statistics")
    assert stats is not None
    assert stats.findtext("Statistics_db-num") == "2"
    assert stats.findtext("Statistics_db-len") == "3000"
    assert stats.findtext("Statistics_hsp-len") == "1"
    assert stats.findtext("Statistics_eff-space") == "26082"
    assert [node.text for node in root.findall(".//Hsp_evalue")] == [
        "1.25e-20",
        "2.5e-10",
    ]
    report = json.loads(report_json.read_text())
    assert report["statistics_equivalence"] == "web_blast_exact"
    assert report["web_blast_statistical_context"] == statistics


def test_merge_sharded_results_supports_outfmt7_tabular(tmp_path: Path) -> None:
    """outfmt 7 merges through the tabular path: per-shard comment lines are
    skipped and a single merged comment header is re-emitted, with the report
    recording outfmt 7."""
    input_tsv = tmp_path / "hits.tsv"
    output_gz = tmp_path / "merged.out.gz"
    report_json = tmp_path / "merge-report.json"
    # Interleave outfmt-7-style comment lines with the 12-column data rows to
    # prove the merge skips them and still merges the data correctly.
    input_tsv.write_text(
        "\n".join(
            [
                "# BLASTN 2.17.0+",
                "# Query: q1",
                "# Database: child-db",
                "# Fields: query acc.ver, subject acc.ver, % identity, alignment length, "
                "mismatches, gap opens, q. start, q. end, s. start, s. end, evalue, bit score",
                "# 2 hits found",
                "q1\ts2\t100\t20\t0\t0\t1\t20\t1\t20\t1e-20\t80",
                "q1\ts1\t100\t20\t0\t0\t1\t20\t1\t20\t1e-30\t90",
                "q2\ts4\t100\t20\t0\t0\t1\t20\t1\t20\t1e-5\t60",
            ]
        )
        + "\n"
    )

    subprocess.run(  # noqa: S603 -- test executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_json),
            "2",
            "blastn",
            "-outfmt 7 -max_target_seqs 2",
        ],
        check=True,
    )

    with gzip.open(output_gz, "rt") as handle:
        lines = [line.rstrip("\n") for line in handle if line.strip()]
    data_rows = [line for line in lines if not line.startswith("#")]
    comment_rows = [line for line in lines if line.startswith("#")]
    # Data is merged + re-ranked by evalue/bitscore (best first per query).
    assert data_rows == [
        "q1\ts1\t100\t20\t0\t0\t1\t20\t1\t20\t1e-30\t90",
        "q1\ts2\t100\t20\t0\t0\t1\t20\t1\t20\t1e-20\t80",
        "q2\ts4\t100\t20\t0\t0\t1\t20\t1\t20\t1e-5\t60",
    ]
    # outfmt 7 comment headers are re-emitted (per query block).
    assert any(line.startswith("# Query:") for line in comment_rows)
    assert any(line.startswith("# Fields:") for line in comment_rows)

    report = json.loads(report_json.read_text())
    assert report["outfmt"] == 7
    assert report["format"] == "blast_tabular"
    assert report["total_output_hits"] == 3


def test_merge_sharded_results_outfmt7_extended_fields_header(tmp_path: Path) -> None:
    """A `7 std staxids ...` run preserves the extended columns AND re-emits the
    authoritative `# Fields:` header BLAST wrote, so the merged output stays
    self-describing instead of mislabelling the trailing columns as bare std."""
    input_tsv = tmp_path / "hits.tsv"
    output_gz = tmp_path / "merged.out.gz"
    report_json = tmp_path / "merge-report.json"
    extended_fields = (
        "query acc.ver, subject acc.ver, % identity, alignment length, mismatches, "
        "gap opens, q. start, q. end, s. start, s. end, evalue, bit score, "
        "subject taxids, subject strand, query seq, subject seq"
    )
    # std 12 columns + staxids + sstrand + qseq + sseq = 16 columns per row.
    row_best = "q1\ts1\t100\t20\t0\t0\t1\t20\t1\t20\t1e-30\t90\t9606\tplus\tACGT\tACGT"
    row_mid = "q1\ts2\t100\t20\t0\t0\t1\t20\t1\t20\t1e-20\t80\t10090\tplus\tACGT\tACGT"
    input_tsv.write_text(
        "\n".join(
            [
                "# BLASTN 2.17.0+",
                "# Query: q1",
                f"# Fields: {extended_fields}",
                "# 2 hits found",
                row_mid,
                row_best,
            ]
        )
        + "\n"
    )

    subprocess.run(  # noqa: S603 -- test executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_json),
            "2",
            "blastn",
            '-outfmt "7 std staxids sstrand qseq sseq" -max_target_seqs 2',
        ],
        check=True,
    )

    with gzip.open(output_gz, "rt") as handle:
        lines = [line.rstrip("\n") for line in handle if line.strip()]
    data_rows = [line for line in lines if not line.startswith("#")]
    # Extended columns are preserved verbatim, re-ranked best-first by evalue.
    assert data_rows == [row_best, row_mid]
    assert all(len(row.split("\t")) == 16 for row in data_rows)
    # The merged header reuses the authoritative extended Fields line, so the
    # trailing taxid / strand / seq columns are correctly described.
    field_lines = [line for line in lines if line.startswith("# Fields:")]
    assert field_lines, "merged output must carry a # Fields: header"
    assert all(line == f"# Fields: {extended_fields}" for line in field_lines)
    assert "subject taxids" in field_lines[0]

    report = json.loads(report_json.read_text())
    assert report["outfmt"] == 7
    assert report["fields"] == extended_fields


def test_merge_sharded_results_outfmt7_reordered_fields_taxids(tmp_path: Path) -> None:
    """The guide's exact taxid layout merges correctly even though it reorders
    columns and omits qseqid.

    `-outfmt "7 sseqid staxids sstrand pident evalue bitscore qstart qend sstart
    send qseq sseq"` puts sseqid at col0, staxids at col1, evalue at col4 and
    bitscore at col5 — none of the historical fixed positions hold. The merge
    must resolve evalue/bitscore BY NAME to re-rank, treat the whole file as one
    query group (no qseqid), and preserve every column including staxids.
    """
    input_tsv = tmp_path / "hits.tsv"
    output_gz = tmp_path / "merged.out.gz"
    report_json = tmp_path / "merge-report.json"
    guide_outfmt = (
        "7 sseqid staxids sstrand pident evalue bitscore qstart qend sstart send qseq sseq"
    )
    # Columns: sseqid, staxids, sstrand, pident, evalue, bitscore, qstart, qend,
    # sstart, send, qseq, sseq  (evalue=col4, bitscore=col5, NO qseqid).
    row_best = "s1\t9606\tplus\t100\t1e-30\t90\t1\t20\t1\t20\tACGT\tACGT"
    row_mid = "s2\t10090\tplus\t100\t1e-20\t80\t1\t20\t1\t20\tACGT\tACGT"
    row_low = "s3\t562\tminus\t98\t1e-5\t60\t1\t20\t1\t20\tACGT\tACGT"
    input_tsv.write_text("\n".join([row_mid, row_best, row_low]) + "\n")

    subprocess.run(  # noqa: S603 -- test executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_json),
            "2",
            "blastn",
            f'-outfmt "{guide_outfmt}" -max_target_seqs 2',
        ],
        check=True,
    )

    with gzip.open(output_gz, "rt") as handle:
        lines = [line.rstrip("\n") for line in handle if line.strip()]
    data_rows = [line for line in lines if not line.startswith("#")]
    # Ranked best-first by the RESOLVED evalue/bitscore columns; max_target_seqs
    # cuts the third hit. staxids (col1) preserved verbatim.
    assert data_rows == [row_best, row_mid]
    assert data_rows[0].split("\t")[1] == "9606"
    assert all(len(row.split("\t")) == 12 for row in data_rows)

    report = json.loads(report_json.read_text())
    assert report["outfmt"] == 7
    # Resolved by name: sseqid=0 (subject), staxids has no role, evalue=4,
    # bitscore=5, and no query column.
    assert report["resolved_columns"] == {
        "qseqid": None,
        "evalue": 4,
        "bitscore": 5,
        "subject": 0,
    }
    # No-query-column fallback is surfaced as a warning.
    assert any("single query group" in w for w in report["warnings"])


def test_merge_sharded_results_outfmt7_unquoted_multitoken(tmp_path: Path) -> None:
    """The canonical wire format is UNQUOTED (quotes break elastic-blast's raw
    YAML substitution), so the merge must resolve the full specifier even when
    `-outfmt` arrives as separate tokens (`-outfmt 7 std staxids`, no quotes)."""
    input_tsv = tmp_path / "hits.tsv"
    output_gz = tmp_path / "merged.out.gz"
    report_json = tmp_path / "merge-report.json"
    # std 12 + staxids = 13 columns; evalue=col10, bitscore=col11 (std positions).
    row_best = "q1\ts1\t100\t20\t0\t0\t1\t20\t1\t20\t1e-30\t90\t9606"
    row_mid = "q1\ts2\t100\t20\t0\t0\t1\t20\t1\t20\t1e-20\t80\t10090"
    input_tsv.write_text("\n".join([row_mid, row_best]) + "\n")

    subprocess.run(  # noqa: S603 -- test executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_json),
            "2",
            "blastn",
            # UNQUOTED multi-token outfmt (the YAML-safe wire format).
            "-outfmt 7 std staxids -max_target_seqs 2",
        ],
        check=True,
    )

    with gzip.open(output_gz, "rt") as handle:
        data_rows = [
            line.rstrip("\n") for line in handle if line.strip() and not line.startswith("#")
        ]
    assert data_rows == [row_best, row_mid]
    assert data_rows[0].split("\t")[12] == "9606"

    report = json.loads(report_json.read_text())
    # The full specifier was recovered (std → positions 0/10/11, staxids extra).
    assert report["resolved_columns"] == {
        "qseqid": 0,
        "evalue": 10,
        "bitscore": 11,
        "subject": 1,
    }


def test_merge_sharded_results_rejects_outfmt_without_rank_columns(tmp_path: Path) -> None:
    """A tabular outfmt missing evalue/bitscore cannot be re-ranked, so the
    merge fails closed rather than emitting an unranked (wrong) result."""
    input_tsv = tmp_path / "hits.tsv"
    output_gz = tmp_path / "merged.out.gz"
    report_json = tmp_path / "merge-report.json"
    input_tsv.write_text("s1\t9606\tplus\t100\n")

    proc = subprocess.run(  # noqa: S603 -- test executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_json),
            "2",
            "blastn",
            '-outfmt "7 sseqid staxids sstrand pident" -max_target_seqs 2',
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "evalue and bitscore" in proc.stderr


def test_query_oracle_is_disabled_without_subject_accession(tmp_path: Path) -> None:
    oracle = tmp_path / "oracle.txt"
    oracle.write_text("s1\n")

    out_rows, report = _run_tabular_merge(
        tmp_path,
        ["q1\t1e-30\t90"],
        num_shards="1",
        max_target_seqs=1,
        outfmt_spec="6 qseqid evalue bitscore",
        env={
            "ELB_TIE_ORDER_FILE": str(oracle),
            "ELB_TIE_ORDER_STRICT": "1",
        },
    )

    assert out_rows == ["q1\t1e-30\t90"]
    assert report["tie_order_oracle_strict"] is False
    assert report["tie_order_oracle_source"] is None
    assert report["selection_equivalence"] == "heuristic"


@pytest.mark.parametrize("num_shards", ["not-an-integer", "0", "1025"])
def test_merge_rejects_invalid_shard_count(tmp_path: Path, num_shards: str) -> None:
    proc = subprocess.run(  # noqa: S603 -- test executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(tmp_path / "hits.tsv"),
            str(tmp_path / "merged.out.gz"),
            str(tmp_path / "merge-report.json"),
            num_shards,
            "blastn",
            "-outfmt 6 std score -max_target_seqs 10",
        ],
        capture_output=True,
        text=True,
    )

    assert proc.returncode != 0
    assert "num_shards" in proc.stderr


def _run_tabular_merge(
    tmp_path: Path,
    rows: list[str],
    *,
    num_shards: str,
    max_target_seqs: int,
    outfmt_spec: str = "6",
    env: dict[str, str] | None = None,
) -> tuple[list[str], dict]:
    input_tsv = tmp_path / "hits.tsv"
    output_gz = tmp_path / "merged.out.gz"
    report_json = tmp_path / "merge-report.json"
    input_tsv.write_text("\n".join(rows) + "\n")
    subprocess.run(  # noqa: S603 -- test executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_json),
            num_shards,
            "blastn",
            f"-outfmt {outfmt_spec} -max_target_seqs {max_target_seqs}",
        ],
        check=True,
        env={**os.environ, **(env or {})},
    )
    with gzip.open(output_gz, "rt") as handle:
        out_rows = [line.strip() for line in handle if line.strip() and not line.startswith("#")]
    return out_rows, json.loads(report_json.read_text())


def _run_xml_merge(
    tmp_path: Path,
    shard_hits: list[list[tuple[str, str, float]]],
    *,
    max_target_seqs: int,
    env: dict[str, str] | None = None,
) -> tuple[list[str], dict]:
    input_tsv = tmp_path / "hits.tsv"
    output_gz = tmp_path / "merged.out.gz"
    report_json = tmp_path / "merge-report.json"
    input_tsv.write_text("")
    for shard_index, hits in enumerate(shard_hits):
        shard_dir = tmp_path / f"shard_{shard_index:02d}"
        shard_dir.mkdir()
        with gzip.open(shard_dir / "batch.out.gz", "wt") as handle:
            handle.write(_blast_xml("q1", hits))
    subprocess.run(  # noqa: S603 -- test executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_json),
            str(len(shard_hits)),
            "blastn",
            f"-outfmt 5 -max_target_seqs {max_target_seqs}",
        ],
        check=True,
        env={**os.environ, **(env or {})},
    )
    with gzip.open(output_gz, "rt") as handle:
        root = ET.parse(handle).getroot()  # noqa: S314 -- test fixture XML
    subjects = [node.text or "" for node in root.findall(".//Hit_id")]
    return subjects, json.loads(report_json.read_text())


def _tabular_row(query: str, subject: str, evalue: str, bitscore: str) -> str:
    return f"{query}\t{subject}\t100\t20\t0\t0\t1\t20\t1\t20\t{evalue}\t{bitscore}"


def test_sequence_diversity_selects_one_representative_per_signature(
    tmp_path: Path,
) -> None:
    rows = [
        "q1\tacc-a\ta-cg\t1\t4\t1e-20\t80\t90",
        "q1\tacc-b\tACG\t1\t4\t1e-30\t70\t85",
        "q1\tacc-a\tTTTT\t1\t4\t1e-10\t60\t70",
    ]

    out_rows, report = _run_tabular_merge(
        tmp_path,
        rows,
        num_shards="2",
        max_target_seqs=2,
        outfmt_spec="6 qseqid saccver sseq qstart qend evalue bitscore score",
        env={
            "ELB_RESULT_SELECTION_POLICY": "sequence_diversity",
            "ELB_REQUESTED_MAX_TARGET_SEQS": "2",
        },
    )

    assert out_rows == [rows[1], rows[2]]
    assert report["result_selection_policy_applied"] == "sequence_diversity"
    assert report["sequence_identity_mode"] == "aligned_sequence_query_span"
    assert report["sequence_identity_version"] == 1
    assert report["observed_candidate_rows"] == 3
    assert report["observed_candidate_subjects"] == 2
    assert report["observed_sequence_groups"] == 2
    assert report["returned_sequence_groups"] == 2


def test_sequence_diversity_accepts_pool_above_legacy_limit(tmp_path: Path) -> None:
    row = "q1\tacc-a\tACGT\t1\t4\t1e-20\t80\t90"

    out_rows, report = _run_tabular_merge(
        tmp_path,
        [row],
        num_shards="1",
        max_target_seqs=5_001,
        outfmt_spec="6 qseqid saccver sseq qstart qend evalue bitscore score",
        env={
            "ELB_RESULT_SELECTION_POLICY": "sequence_diversity",
            "ELB_REQUESTED_MAX_TARGET_SEQS": "1",
        },
    )

    assert out_rows == [row]
    assert report["candidate_pool_size"] == 5_001
    assert report["returned_sequence_groups"] == 1


def test_sequence_diversity_bounds_candidate_pool_saturation_details(
    tmp_path: Path,
) -> None:
    rows = [
        row
        for shard in range(101)
        for row in (
            f"# ELB source-shard:{shard:03d}",
            f"q1\tacc-{shard:03d}\tAAAA{shard:03d}\t1\t4\t1e-20\t80\t90",
        )
    ]

    _out_rows, report = _run_tabular_merge(
        tmp_path,
        rows,
        num_shards="101",
        max_target_seqs=1,
        outfmt_spec="6 qseqid saccver sseq qstart qend evalue bitscore score",
        env={
            "ELB_RESULT_SELECTION_POLICY": "sequence_diversity",
            "ELB_REQUESTED_MAX_TARGET_SEQS": "1",
            "ELB_CANDIDATE_POOL_SIZE_REQUESTED": "1",
        },
    )

    assert report["candidate_pool_saturated_shards"] == 101
    assert len(report["candidate_pool_saturation_details"]) == 100
    assert report["candidate_pool_saturation_details"][0]["source_shard"] == "000"
    assert report["candidate_pool_saturation_details"][-1]["source_shard"] == "099"
    assert report["candidate_pool_saturation_details_truncated"] is True


def test_sequence_diversity_publish_failure_is_atomic(tmp_path: Path) -> None:
    input_tsv = tmp_path / "hits.tsv"
    output_gz = tmp_path / "merged.out.gz"
    report_target = tmp_path / "merge-report.json"
    input_tsv.write_text(
        "# ELB source-shard:00\nq1\tacc-a\tACGT\t1\t4\t1e-20\t80\t90\n"
    )
    report_target.mkdir()

    proc = subprocess.run(  # noqa: S603 -- executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_target),
            "1",
            "blastn",
            "-outfmt 6 qseqid saccver sseq qstart qend evalue bitscore score "
            "-max_target_seqs 1",
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "ELB_RESULT_SELECTION_POLICY": "sequence_diversity",
            "ELB_REQUESTED_MAX_TARGET_SEQS": "1",
            "ELB_SUCCEEDED_SHARDS": "1",
        },
    )

    assert proc.returncode != 0
    assert not output_gz.exists()
    assert report_target.is_dir()
    assert list(tmp_path.glob(".merged.out.gz.merge-tabular-*.sqlite3*")) == []
    assert list(tmp_path.glob(".*.tmp")) == []


def test_sequence_diversity_sigterm_cleans_temporary_artifacts(tmp_path: Path) -> None:
    input_tsv = tmp_path / "hits.tsv"
    output_gz = tmp_path / "merged.out.gz"
    report_json = tmp_path / "merge-report.json"
    sequence = "ACGT" * 64
    with input_tsv.open("w") as handle:
        handle.write("# ELB source-shard:00\n")
        for index in range(20_000):
            handle.write(
                f"q1\tacc-{index:05d}\t{sequence}{index:06d}\t1\t256\t"
                "1e-20\t80\t90\n"
            )

    process = subprocess.Popen(  # noqa: S603 -- executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_json),
            "1",
            "blastn",
            "-outfmt 6 qseqid saccver sseq qstart qend evalue bitscore score "
            "-max_target_seqs 20000",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "ELB_RESULT_SELECTION_POLICY": "sequence_diversity",
            "ELB_REQUESTED_MAX_TARGET_SEQS": "20000",
            "ELB_SUCCEEDED_SHARDS": "1",
        },
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and process.poll() is None:
        if list(tmp_path.glob(".merged.out.gz.merge-tabular-*.sqlite3*")):
            break
        time.sleep(0.01)
    assert list(tmp_path.glob(".merged.out.gz.merge-tabular-*.sqlite3*"))

    process.terminate()
    process.communicate(timeout=10)

    assert process.returncode != 0
    assert not output_gz.exists()
    assert not report_json.exists()
    assert list(tmp_path.glob(".merged.out.gz.merge-tabular-*.sqlite3*")) == []
    assert list(tmp_path.glob(".*.tmp")) == []
    lock_path = Path(f"{output_gz}.lock")
    with lock_path.open("r+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def test_sequence_diversity_recovers_orphans_after_sigkill(tmp_path: Path) -> None:
    input_tsv = tmp_path / "hits.tsv"
    output_gz = tmp_path / "merged.out.gz"
    report_json = tmp_path / "merge-report.json"
    sequence = "ACGT" * 64
    with input_tsv.open("w") as handle:
        handle.write("# ELB source-shard:00\n")
        for index in range(20_000):
            handle.write(
                f"q1\tacc-{index:05d}\t{sequence}{index:06d}\t1\t256\t"
                "1e-20\t80\t90\n"
            )

    process = subprocess.Popen(  # noqa: S603 -- executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_json),
            "1",
            "blastn",
            "-outfmt 6 qseqid saccver sseq qstart qend evalue bitscore score "
            "-max_target_seqs 20000",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env={
            **os.environ,
            "ELB_RESULT_SELECTION_POLICY": "sequence_diversity",
            "ELB_REQUESTED_MAX_TARGET_SEQS": "20000",
            "ELB_SUCCEEDED_SHARDS": "1",
        },
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and process.poll() is None:
        if list(tmp_path.glob(".merged.out.gz.merge-tabular-*.sqlite3*")):
            break
        time.sleep(0.01)
    assert list(tmp_path.glob(".merged.out.gz.merge-tabular-*.sqlite3*"))

    os.killpg(process.pid, signal.SIGKILL)
    process.communicate(timeout=10)
    assert process.returncode != 0
    assert list(tmp_path.glob(".merged.out.gz.merge-tabular-*.sqlite3*"))
    unrelated_temp = tmp_path / ".other-output.gz.abcdefgh.tmp"
    unrelated_temp.write_text("keep")
    prefixed_sibling_output = tmp_path / ".merged.out.gz.sibling.abcdefgh.tmp"
    prefixed_sibling_output.write_text("keep")
    prefixed_sibling_report = tmp_path / ".merge-report.json.sibling.abcdefgh.tmp"
    prefixed_sibling_report.write_text("keep")

    input_tsv.write_text(
        "# ELB source-shard:00\nq1\tacc-a\tACGT\t1\t4\t1e-20\t80\t90\n"
    )
    retry = subprocess.run(  # noqa: S603 -- executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_json),
            "1",
            "blastn",
            "-outfmt 6 qseqid saccver sseq qstart qend evalue bitscore score "
            "-max_target_seqs 1",
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "ELB_RESULT_SELECTION_POLICY": "sequence_diversity",
            "ELB_REQUESTED_MAX_TARGET_SEQS": "1",
            "ELB_SUCCEEDED_SHARDS": "1",
        },
    )

    assert retry.returncode == 0, retry.stderr
    assert list(tmp_path.glob(".merged.out.gz.merge-tabular-*.sqlite3*")) == []
    assert list(tmp_path.glob(".merged.out.gz.*.tmp")) == [prefixed_sibling_output]
    assert list(tmp_path.glob(".merge-report.json.*.tmp")) == [
        prefixed_sibling_report
    ]
    assert unrelated_temp.read_text() == "keep"
    assert prefixed_sibling_output.read_text() == "keep"
    assert prefixed_sibling_report.read_text() == "keep"


@pytest.mark.slow
def test_sequence_diversity_large_pool_uses_disk_backed_bounded_memory(
    tmp_path: Path,
) -> None:
    input_tsv = tmp_path / "sequence-hits.tsv"
    output_gz = tmp_path / "merged.out.gz"
    report_json = tmp_path / "merge-report.json"
    sequence = "A" * 16_384
    with input_tsv.open("w") as handle:
        handle.write("# ELB source-shard:00\n")
        for index in range(6_000):
            handle.write(
                f"q1\tacc-{index:05d}\t{sequence}{index:06d}\t1\t16384\t"
                "1e-20\t80\t90\n"
            )

    memory_limit = 96 * 1024 * 1024

    def limit_address_space() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))

    proc = subprocess.run(  # noqa: S603 -- executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_json),
            "1",
            "blastn",
            "-outfmt 6 qseqid saccver sseq qstart qend evalue bitscore score "
            "-max_target_seqs 6000",
        ],
        capture_output=True,
        text=True,
        preexec_fn=limit_address_space,
        env={
            **os.environ,
            "ELB_RESULT_SELECTION_POLICY": "sequence_diversity",
            "ELB_REQUESTED_MAX_TARGET_SEQS": "6000",
            "ELB_SUCCEEDED_SHARDS": "1",
        },
    )

    assert proc.returncode == 0, proc.stderr
    with gzip.open(output_gz, "rt") as handle:
        output_rows = sum(1 for line in handle if line.strip() and not line.startswith("#"))
    report = json.loads(report_json.read_text())
    assert output_rows == 6_000
    assert report["observed_candidate_rows"] == 6_000
    assert report["observed_sequence_groups"] == 6_000
    assert report["returned_sequence_groups"] == 6_000
    assert len(report["sequence_group_counts"]) == 5_000
    assert report["sequence_group_counts_truncated"] is True


def test_sequence_diversity_rejects_xml_at_merge_boundary(tmp_path: Path) -> None:
    input_tsv = tmp_path / "hits.tsv"
    input_tsv.write_text("")
    proc = subprocess.run(  # noqa: S603 -- test executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(tmp_path / "merged.out.gz"),
            str(tmp_path / "merge-report.json"),
            "1",
            "blastn",
            "-outfmt 5 -max_target_seqs 2",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "ELB_RESULT_SELECTION_POLICY": "sequence_diversity"},
    )

    assert proc.returncode != 0
    assert "supports only tabular BLAST outfmt 6 or 7" in proc.stderr


def test_deterministic_tie_order_off_preserves_ordinal(tmp_path: Path) -> None:
    rows = [
        _tabular_row("q1", "s2", "1e-30", "90"),
        _tabular_row("q1", "s3", "1e-30", "90"),
        _tabular_row("q1", "s1", "1e-30", "90"),
    ]
    out_rows, report = _run_tabular_merge(tmp_path, rows, num_shards="3", max_target_seqs=10)
    assert [row.split("\t")[1] for row in out_rows] == ["s2", "s3", "s1"]
    assert report["ranking_basis"] == "evalue_bitscore_ordinal"


def test_deterministic_tie_order_on_sorts_by_accession(tmp_path: Path) -> None:
    rows = [
        _tabular_row("q1", "s2", "1e-30", "90"),
        _tabular_row("q1", "s3", "1e-30", "90"),
        _tabular_row("q1", "s1", "1e-30", "90"),
    ]
    out_rows, report = _run_tabular_merge(
        tmp_path,
        rows,
        num_shards="3",
        max_target_seqs=10,
        env={"ELB_DETERMINISTIC_TIE_ORDER": "1"},
    )
    assert [row.split("\t")[1] for row in out_rows] == ["s1", "s2", "s3"]
    assert report["ranking_basis"] == "evalue_bitscore_accession_ordinal"


def test_tabular_max_target_seqs_counts_subjects_and_preserves_hsps(
    tmp_path: Path,
) -> None:
    first_hsp = _tabular_row("q1", "s1", "1e-30", "90")
    second_hsp = _tabular_row("q1", "s1", "1e-10", "70")
    second_subject = _tabular_row("q1", "s2", "1e-20", "80")
    excluded_subject = _tabular_row("q1", "s3", "1e-5", "60")

    out_rows, report = _run_tabular_merge(
        tmp_path,
        [first_hsp, second_hsp, second_subject, excluded_subject],
        num_shards="2",
        max_target_seqs=2,
    )

    assert out_rows == [first_hsp, second_hsp, second_subject]
    assert report["total_input_hits"] == 4
    assert report["total_input_rows"] == 4
    assert report["total_input_subjects"] == 3
    assert report["total_output_hits"] == 3
    assert report["total_output_rows"] == 3
    assert report["total_output_subjects"] == 2


def test_requested_max_target_seqs_caps_widened_candidate_pool(tmp_path: Path) -> None:
    rows = [
        _tabular_row("q1", "s1", "1e-30", "90"),
        _tabular_row("q1", "s2", "1e-20", "80"),
        _tabular_row("q1", "s3", "1e-10", "70"),
        _tabular_row("q1", "s4", "1e-5", "60"),
    ]

    out_rows, report = _run_tabular_merge(
        tmp_path,
        rows,
        num_shards="2",
        max_target_seqs=4,
        env={"ELB_REQUESTED_MAX_TARGET_SEQS": "2"},
    )

    assert [row.split("\t")[1] for row in out_rows] == ["s1", "s2"]
    assert report["max_target_seqs"] == 2
    assert report["candidate_pool_size"] == 4
    assert report["total_output_subjects"] == 2


def test_requested_max_target_seqs_caps_xml_candidate_pool(tmp_path: Path) -> None:
    subjects, report = _run_xml_merge(
        tmp_path,
        [
            [("s1", "1e-30", 90.0), ("s3", "1e-10", 70.0)],
            [("s2", "1e-20", 80.0), ("s4", "1e-5", 60.0)],
        ],
        max_target_seqs=4,
        env={"ELB_REQUESTED_MAX_TARGET_SEQS": "2"},
    )

    assert subjects == ["s1", "s2"]
    assert report["max_target_seqs"] == 2
    assert report["candidate_pool_size"] == 4
    assert report["total_output_subjects"] == 2


@pytest.mark.slow
def test_large_sseq_rows_merge_under_bounded_memory(tmp_path: Path) -> None:
    input_tsv = tmp_path / "long-sequences.tsv"
    output_gz = tmp_path / "merged.out.gz"
    report_json = tmp_path / "merge-report.json"
    sequence = "A" * 16_384
    with input_tsv.open("w") as handle:
        for index in range(5_000):
            handle.write(
                f"q1\ts{index:05d}\t1\tname\ttitle\t16384\t99\t16384\t16384\t0\t"
                f"1e-20\t80\t1\t16384\t1\t16384\t100\t{sequence}\t1\tname\t100\n"
            )

    memory_limit = 96 * 1024 * 1024

    def limit_address_space() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))

    proc = subprocess.run(  # noqa: S603 -- test executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_json),
            "10",
            "blastn",
            "-outfmt 7 qseqid saccver staxid ssciname stitle slen pident length "
            "nident gaps evalue bitscore qstart qend sstart send qcovhsp sseq "
            "staxids sscinames qcovs -max_target_seqs 100",
        ],
        capture_output=True,
        text=True,
        preexec_fn=limit_address_space,
    )

    assert proc.returncode == 0, proc.stderr
    report = json.loads(report_json.read_text())
    assert report["total_input_rows"] == 5_000
    assert report["total_input_subjects"] == 5_000
    assert report["total_output_rows"] == 100
    assert report["total_output_subjects"] == 100


@pytest.mark.slow
def test_large_db_order_oracle_streams_under_bounded_memory(tmp_path: Path) -> None:
    input_tsv = tmp_path / "hits.tsv"
    output_gz = tmp_path / "merged.out.gz"
    report_json = tmp_path / "merge-report.json"
    oracle = tmp_path / "db-order.txt"
    input_tsv.write_text(
        "q1\tselected-a\t1e-30\t90\t100\n"
        "q1\tselected-b\t1e-30\t90\t100\n"
    )
    unrelated_count = 1_000_000
    with oracle.open("w") as handle:
        for index in range(unrelated_count):
            handle.write(f"00\t{index}\tunused-{index}\n")
        handle.write(f"00\t{unrelated_count}\tselected-a\n")
        handle.write(f"00\t{unrelated_count + 1}\tselected-b\n")

    memory_limit = 96 * 1024 * 1024

    def limit_address_space() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))

    proc = subprocess.run(  # noqa: S603 -- test executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_json),
            "2",
            "blastn",
            "-outfmt 6 qseqid sseqid evalue bitscore score -max_target_seqs 1",
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "ELB_TIE_ORDER_FILE": str(oracle),
            "ELB_TIE_ORDER_SOURCE": "db_order",
        },
        preexec_fn=limit_address_space,
    )

    assert proc.returncode == 0, proc.stderr
    with gzip.open(output_gz, "rt") as handle:
        rows = [line.strip() for line in handle if line.strip() and not line.startswith("#")]
    assert [row.split("\t")[1] for row in rows] == ["selected-b"]
    report = json.loads(report_json.read_text())
    assert report["tie_order_oracle_accessions"] == unrelated_count + 2
    assert report["selection_equivalence"] == "full_db_hitlist_exact"
    assert any("streamed" in warning for warning in report["warnings"])


@pytest.mark.parametrize("requested", ["0", "5", "invalid"])
def test_requested_max_target_seqs_rejects_invalid_cap(
    tmp_path: Path,
    requested: str,
) -> None:
    input_tsv = tmp_path / "hits.tsv"
    output_gz = tmp_path / "merged.out.gz"
    report_json = tmp_path / "merge-report.json"
    input_tsv.write_text(_tabular_row("q1", "s1", "1e-30", "90") + "\n")

    proc = subprocess.run(  # noqa: S603 -- test executes the checked-in merge helper
        [
            "/bin/bash",
            str(SCRIPT),
            str(input_tsv),
            str(output_gz),
            str(report_json),
            "1",
            "blastn",
            "-outfmt 6 -max_target_seqs 4",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "ELB_REQUESTED_MAX_TARGET_SEQS": requested},
    )

    assert proc.returncode != 0
    assert "requested max_target_seqs" in proc.stderr


def test_diversity_aware_cutoff_zero_preserves_strict_top_n(tmp_path: Path) -> None:
    rows = [
        _tabular_row("q1", "pa", "1e-30", "90"),
        _tabular_row("q1", "pb", "1e-30", "90"),
        _tabular_row("q1", "pc", "1e-30", "90"),
        _tabular_row("q1", "pd", "1e-30", "90"),
        _tabular_row("q1", "na", "1e-20", "80"),
        _tabular_row("q1", "nb", "1e-20", "80"),
    ]
    out_rows, report = _run_tabular_merge(
        tmp_path,
        rows,
        num_shards="6",
        max_target_seqs=3,
        env={"ELB_DIVERSITY_AWARE_CUTOFF": "0"},
    )
    assert [row.split("\t")[1] for row in out_rows] == ["pa", "pb", "pc"]
    assert report["diversity_reserved_count"] == 0
    assert report["diversity_candidate_count"] == 0
    assert report["diversity_reservation_mode"] == "off"
    assert report["tie_cutoff_overflow_count"] == 1


def test_diversity_aware_cutoff_defaults_to_proportional_near_misses(
    tmp_path: Path,
) -> None:
    rows = [
        _tabular_row("q1", "pa", "1e-30", "90"),
        _tabular_row("q1", "pb", "1e-30", "90"),
        _tabular_row("q1", "pc", "1e-30", "90"),
        _tabular_row("q1", "pd", "1e-30", "90"),
        _tabular_row("q1", "pa", "1e-25", "85"),
        _tabular_row("q1", "na", "1e-20", "80"),
        _tabular_row("q1", "nb", "1e-20", "80"),
    ]
    out_rows, report = _run_tabular_merge(
        tmp_path,
        rows,
        num_shards="6",
        max_target_seqs=3,
    )
    assert [row.split("\t")[1] for row in out_rows] == ["pa", "pa", "pb", "na"]
    assert report["total_output_hits"] == 4
    assert report["total_output_rows"] == 4
    assert report["total_output_subjects"] == 3
    assert report["diversity_reserved_count"] == 1
    assert report["diversity_candidate_count"] == 2
    assert report["diversity_reservation_mode"] == "proportional"
    assert report["diversity_queries"] == [
        {
            "query_id": "q1",
            "candidate_count": 2,
            "reservation_mode": "proportional",
            "reserved_count": 1,
        }
    ]
    # The truncation signal is still reported against the real top score class.
    assert report["tie_cutoff_overflow_count"] == 1
    assert any("Diversity-aware cutoff" in warning for warning in report["warnings"])


def test_diversity_aware_cutoff_requires_top_class_overflow(tmp_path: Path) -> None:
    rows = [
        _tabular_row("q1", "pa", "1e-30", "90"),
        _tabular_row("q1", "pb", "1e-30", "90"),
        _tabular_row("q1", "pc", "1e-30", "90"),
        _tabular_row("q1", "na", "1e-20", "80"),
    ]
    out_rows, report = _run_tabular_merge(
        tmp_path,
        rows,
        num_shards="4",
        max_target_seqs=3,
    )
    assert [row.split("\t")[1] for row in out_rows] == ["pa", "pb", "pc"]
    assert report["tie_cutoff_overflow_count"] == 0
    assert report["diversity_reserved_count"] == 0


def test_diversity_aware_cutoff_preserves_multiple_variants_at_5000(
    tmp_path: Path,
) -> None:
    rows = [
        *(_tabular_row("q1", f"perfect-{idx:05d}", "1e-30", "90") for idx in range(6000)),
        *(_tabular_row("q1", f"variant-{idx:05d}", "1e-20", "80") for idx in range(4000)),
    ]

    out_rows, report = _run_tabular_merge(
        tmp_path,
        rows,
        num_shards="10",
        max_target_seqs=5000,
    )

    subjects = [row.split("\t")[1] for row in out_rows]
    assert len(subjects) == 5000
    assert sum(subject.startswith("perfect-") for subject in subjects) == 3000
    assert sum(subject.startswith("variant-") for subject in subjects) == 2000
    assert subjects[-1] == "variant-01999"
    assert report["diversity_reserved_count"] == 2000
    assert report["diversity_candidate_count"] == 4000
    assert report["diversity_reservation_mode"] == "proportional"
    assert report["tie_cutoff_overflow_count"] == 1000


def test_diversity_aware_cutoff_keeps_the_only_top_slot(tmp_path: Path) -> None:
    rows = [
        _tabular_row("q1", "pa", "1e-30", "90"),
        _tabular_row("q1", "pb", "1e-30", "90"),
        _tabular_row("q1", "na", "1e-20", "80"),
    ]
    out_rows, report = _run_tabular_merge(
        tmp_path,
        rows,
        num_shards="3",
        max_target_seqs=1,
    )
    assert [row.split("\t")[1] for row in out_rows] == ["pa"]
    assert report["tie_cutoff_overflow_count"] == 1
    assert report["diversity_reserved_count"] == 0


def test_strict_tie_order_oracle_disables_default_diversity_reservation(
    tmp_path: Path,
) -> None:
    oracle = tmp_path / "oracle.txt"
    oracle.write_text("pa\npb\npc\npd\nna\n")
    rows = [
        _tabular_row("q1", "pa", "1e-30", "90"),
        _tabular_row("q1", "pb", "1e-30", "90"),
        _tabular_row("q1", "pc", "1e-30", "90"),
        _tabular_row("q1", "pd", "1e-30", "90"),
        _tabular_row("q1", "na", "1e-20", "80"),
    ]
    out_rows, report = _run_tabular_merge(
        tmp_path,
        rows,
        num_shards="5",
        max_target_seqs=3,
        env={
            "ELB_TIE_ORDER_FILE": str(oracle),
            "ELB_TIE_ORDER_STRICT": "1",
        },
    )
    assert [row.split("\t")[1] for row in out_rows] == ["pa", "pb", "pc"]
    assert report["tie_order_oracle_strict"] is True
    assert report["diversity_reserved_count"] == 0
    assert report["diversity_reservation_mode"] == "strict_oracle"


def test_diversity_aware_cutoff_positive_value_sets_reservation(tmp_path: Path) -> None:
    rows = [
        _tabular_row("q1", "pa", "1e-30", "90"),
        _tabular_row("q1", "pb", "1e-30", "90"),
        _tabular_row("q1", "pc", "1e-30", "90"),
        _tabular_row("q1", "pd", "1e-30", "90"),
        _tabular_row("q1", "na", "1e-20", "80"),
        _tabular_row("q1", "nb", "1e-20", "80"),
    ]
    out_rows, report = _run_tabular_merge(
        tmp_path,
        rows,
        num_shards="6",
        max_target_seqs=3,
        env={"ELB_DIVERSITY_AWARE_CUTOFF": "2"},
    )
    assert [row.split("\t")[1] for row in out_rows] == ["pa", "na", "nb"]
    assert report["diversity_reserved_count"] == 2
    assert report["diversity_candidate_count"] == 2
    assert report["diversity_reservation_mode"] == "fixed"


def test_diversity_aware_cutoff_without_subject_operates_on_rows(
    tmp_path: Path,
) -> None:
    rows = [
        "q1\t1e-30\t90",
        "q1\t1e-30\t90",
        "q1\t1e-30\t90",
        "q1\t1e-30\t90",
        "q1\t1e-20\t80",
        "q1\t1e-20\t80",
    ]

    out_rows, report = _run_tabular_merge(
        tmp_path,
        rows,
        num_shards="2",
        max_target_seqs=3,
        outfmt_spec="6 qseqid evalue bitscore",
    )

    assert [row.split("\t")[1] for row in out_rows] == ["1e-30", "1e-30", "1e-20"]
    assert report["diversity_reserved_count"] == 1
    assert report["diversity_candidate_count"] == 2
    assert report["total_output_subjects"] == 3
    assert any("operate on rows instead of subjects" in warning for warning in report["warnings"])


def test_xml_diversity_aware_cutoff_defaults_to_proportional_near_misses(
    tmp_path: Path,
) -> None:
    subjects, report = _run_xml_merge(
        tmp_path,
        [
            [("pa", "1e-30", 90.0), ("pb", "1e-30", 90.0)],
            [
                ("pc", "1e-30", 90.0),
                ("pd", "1e-30", 90.0),
                ("na", "1e-20", 80.0),
                ("nb", "1e-20", 80.0),
            ],
        ],
        max_target_seqs=3,
    )
    assert subjects == ["pa", "pb", "na"]
    assert report["diversity_reserved_count"] == 1
    assert report["diversity_candidate_count"] == 2
    assert report["diversity_reservation_mode"] == "proportional"
    assert report["diversity_queries"] == [
        {
            "query_id": "q1",
            "candidate_count": 2,
            "reservation_mode": "proportional",
            "reserved_count": 1,
        }
    ]
    assert report["tie_cutoff_overflow_count"] == 1
    assert any("Diversity-aware cutoff" in warning for warning in report["warnings"])
