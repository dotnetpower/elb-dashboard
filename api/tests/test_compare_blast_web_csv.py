"""Tests for Compare BLAST Web Csv behavior.

Responsibility: Tests for Compare BLAST Web Csv behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_run`, `test_compare_web_csv_to_outfmt6_accepts_equivalent_rows`,
`test_compare_web_csv_to_outfmt6_reports_snapshot_mismatch`,
`test_compare_web_csv_to_outfmt6_reports_tie_window_equivalence`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_compare_blast_web_csv.py`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.subprocess

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "compare-blast-web-csv.py"


def _run(
    web_csv: Path,
    candidate: Path,
    report: Path,
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- test executes checked-in dev script.
        [
            sys.executable,
            str(SCRIPT),
            "--web-csv",
            str(web_csv),
            "--candidate",
            str(candidate),
            "--json",
            str(report),
            *extra,
        ],
        check=False,
        text=True,
        capture_output=True,
    )


def test_compare_web_csv_to_outfmt6_accepts_equivalent_rows(tmp_path: Path) -> None:
    web_csv = tmp_path / "web.csv"
    candidate = tmp_path / "candidate.out"
    report = tmp_path / "report.json"
    web_csv.write_text(
        "accession,scientific_name,taxid,hit_def,identity_pct,coverage_pct,evalue,bits,align_length,identities,gaps,query_from,query_to,hit_from,hit_to,subject_length\n"
        "ABC123.1,Example,1,subject,100.0,100.0,0.0,828.419,462,462,0,1,462,10,471,1000\n",
        encoding="utf-8",
    )
    candidate.write_text(
        "query\tABC123.1\t100.000\t462\t0\t0\t1\t462\t10\t471\t0.0\t828.419\n",
        encoding="utf-8",
    )

    result = _run(web_csv, candidate, report, "--query-id", "query")

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(report.read_text())
    assert payload["equivalent"] is True
    assert payload["shared_accessions"] == 1
    assert payload["value_mismatch_count"] == 0


def test_compare_web_csv_to_outfmt6_reports_snapshot_mismatch(tmp_path: Path) -> None:
    web_csv = tmp_path / "web.csv"
    candidate = tmp_path / "candidate.out"
    report = tmp_path / "report.json"
    web_csv.write_text(
        "accession,scientific_name,taxid,hit_def,identity_pct,coverage_pct,evalue,bits,align_length,identities,gaps,query_from,query_to,hit_from,hit_to,subject_length\n"
        "WEB001.1,Example,1,subject,100.0,100.0,0.0,828.419,462,462,0,1,462,10,471,1000\n",
        encoding="utf-8",
    )
    candidate.write_text(
        "query\tOLD001.1\t100.000\t462\t0\t0\t1\t462\t10\t471\t0.0\t828.419\n",
        encoding="utf-8",
    )

    result = _run(web_csv, candidate, report, "--query-id", "query")

    assert result.returncode == 1
    payload = json.loads(report.read_text())
    assert payload["equivalent"] is False
    assert payload["shared_accessions"] == 0
    assert payload["first_order_mismatch"] == {
        "rank": 1,
        "web": "WEB001.1",
        "candidate": "OLD001.1",
    }


def test_compare_web_csv_to_outfmt6_reports_tie_window_equivalence(tmp_path: Path) -> None:
    web_csv = tmp_path / "web.csv"
    candidate = tmp_path / "candidate.out"
    strict_report = tmp_path / "strict-report.json"
    tie_report = tmp_path / "tie-report.json"
    web_csv.write_text(
        "accession,scientific_name,taxid,hit_def,identity_pct,coverage_pct,evalue,bits,align_length,identities,gaps,query_from,query_to,hit_from,hit_to,subject_length\n"
        "AAA111.1,Example,1,subject,100.0,100.0,0.0,180.5,100,100,0,1,100,1,100,1000\n"
        "BBB222.1,Example,1,subject,100.0,100.0,0.0,180.5,100,100,0,1,100,1,100,1000\n",
        encoding="utf-8",
    )
    candidate.write_text(
        "\n".join(
            [
                "query\tCCC333.1\t100.000\t100\t0\t0\t1\t100\t1\t100\t0.0\t180.5",
                "query\tBBB222.1\t100.000\t100\t0\t0\t1\t100\t1\t100\t0.0\t180.5",
                "query\tAAA111.1\t100.000\t100\t0\t0\t1\t100\t1\t100\t0.0\t180.5",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    strict_result = _run(web_csv, candidate, strict_report, "--query-id", "query")
    tie_result = _run(
        web_csv,
        candidate,
        tie_report,
        "--query-id",
        "query",
        "--accept-tie-window",
    )

    assert strict_result.returncode == 1
    assert tie_result.returncode == 0, tie_result.stdout + tie_result.stderr
    payload = json.loads(tie_report.read_text())
    assert payload["equivalent"] is False
    assert payload["tie_window_equivalent"] is True
    assert payload["tie_window"]["web_rows_missing_from_candidate_pool"] == 0
    assert payload["tie_window"]["web_rows_with_value_mismatch"] == 0
