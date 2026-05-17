from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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
