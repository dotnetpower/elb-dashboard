"""Tests for Sharded Merge behavior.

Responsibility: Tests for Sharded Merge behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_blast_xml`, `test_merge_sharded_results_respects_top_n_and_report`,
`test_merge_sharded_results_reports_ties`,
`test_merge_sharded_results_reports_tie_cutoff_overflow`,
`test_merge_sharded_results_uses_tie_order_oracle`,
`test_merge_sharded_results_strict_oracle_excludes_non_oracle_hits`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_sharded_merge.py`.
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "terminal" / "merge-sharded-results.sh"


def _blast_xml(
    query_id: str,
    hits: list[tuple[str, str, float]],
    *,
    db_len: int = 1000,
    db_num: int = 1,
    eff_space: int = 17928,
    hsp_len: int = 1,
) -> str:
    hit_xml = []
    for index, (subject, evalue, bitscore) in enumerate(hits, start=1):
        hit_xml.append(
            f"""        <Hit>
          <Hit_num>{index}</Hit_num>
          <Hit_id>{subject}</Hit_id>
          <Hit_def>{subject}</Hit_def>
          <Hit_hsps>
            <Hsp>
              <Hsp_num>1</Hsp_num>
              <Hsp_bit-score>{bitscore}</Hsp_bit-score>
              <Hsp_score>{int(bitscore)}</Hsp_score>
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
