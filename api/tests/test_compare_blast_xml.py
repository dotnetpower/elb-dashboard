"""Tests for Compare BLAST Xml behavior.

Responsibility: Tests for Compare BLAST Xml behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_xml`, `_run`, `test_compare_blast_xml_ignores_provenance_db_path`,
`test_compare_blast_xml_detects_hsp_mismatch`,
`test_compare_blast_xml_can_require_normalized_db_match`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_compare_blast_xml.py`.
"""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "compare-blast-xml.py"


def _xml(*, db: str, evalue: str = "1e-30", qseq: str = "ACGT", hseq: str = "ACGT") -> str:
    return f"""<?xml version="1.0"?>
<BlastOutput>
  <BlastOutput_program>blastn</BlastOutput_program>
  <BlastOutput_version>BLASTN 2.17.0+</BlastOutput_version>
  <BlastOutput_reference>reference text</BlastOutput_reference>
  <BlastOutput_db>{db}</BlastOutput_db>
  <BlastOutput_iterations>
    <Iteration>
      <Iteration_query-ID>Query_1</Iteration_query-ID>
      <Iteration_query-def>calibration_query</Iteration_query-def>
      <Iteration_query-len>4</Iteration_query-len>
      <Iteration_hits>
        <Hit>
          <Hit_id>subject_1</Hit_id>
          <Hit_def>subject one</Hit_def>
          <Hit_accession>subject_1</Hit_accession>
          <Hit_len>4</Hit_len>
          <Hit_hsps>
            <Hsp>
              <Hsp_bit-score>80.00</Hsp_bit-score>
              <Hsp_score>40</Hsp_score>
              <Hsp_evalue>{evalue}</Hsp_evalue>
              <Hsp_query-from>1</Hsp_query-from>
              <Hsp_query-to>4</Hsp_query-to>
              <Hsp_hit-from>1</Hsp_hit-from>
              <Hsp_hit-to>4</Hsp_hit-to>
              <Hsp_identity>4</Hsp_identity>
              <Hsp_gaps>0</Hsp_gaps>
              <Hsp_align-len>4</Hsp_align-len>
              <Hsp_qseq>{qseq}</Hsp_qseq>
              <Hsp_hseq>{hseq}</Hsp_hseq>
              <Hsp_midline>||||</Hsp_midline>
            </Hsp>
          </Hit_hsps>
        </Hit>
      </Iteration_hits>
      <Iteration_stat>
        <Statistics>
          <Statistics_db-num>1</Statistics_db-num>
          <Statistics_db-len>4</Statistics_db-len>
          <Statistics_hsp-len>0</Statistics_hsp-len>
          <Statistics_eff-space>16</Statistics_eff-space>
          <Statistics_kappa>0.46</Statistics_kappa>
          <Statistics_lambda>1.28</Statistics_lambda>
          <Statistics_entropy>0.85</Statistics_entropy>
        </Statistics>
      </Iteration_stat>
    </Iteration>
  </BlastOutput_iterations>
</BlastOutput>
"""


def _run(left: Path, right: Path, report: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- test executes checked-in dev script.
        [
            sys.executable,
            str(SCRIPT),
            "--left",
            str(left),
            "--right",
            str(right),
            "--json",
            str(report),
            *extra,
        ],
        check=False,
        text=True,
        capture_output=True,
    )


def test_compare_blast_xml_ignores_provenance_db_path(tmp_path: Path) -> None:
    left = tmp_path / "left.xml"
    right = tmp_path / "right.xml.gz"
    report = tmp_path / "report.json"
    left.write_text(_xml(db="/mnt/blast/core_nt", evalue="1.0e-30"), encoding="utf-8")
    with gzip.open(right, "wt") as handle:
        handle.write(_xml(db="core_nt", evalue="1e-30"))

    result = _run(left, right, report)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(report.read_text())
    assert payload["equivalent"] is True
    assert payload["ignored_provenance_fields"] == ["BlastOutput_db"]
    assert payload["left_summary"]["db_normalized"] == "core_nt"


def test_compare_blast_xml_detects_hsp_mismatch(tmp_path: Path) -> None:
    left = tmp_path / "left.xml"
    right = tmp_path / "right.xml"
    report = tmp_path / "report.json"
    left.write_text(_xml(db="core_nt"), encoding="utf-8")
    right.write_text(_xml(db="core_nt", hseq="ACGA"), encoding="utf-8")

    result = _run(left, right, report)

    assert result.returncode == 1
    payload = json.loads(report.read_text())
    assert payload["equivalent"] is False
    assert payload["difference_count"] == 1
    assert payload["differences"][0]["path"].endswith("Hsp_hseq")


def test_compare_blast_xml_can_require_normalized_db_match(tmp_path: Path) -> None:
    left = tmp_path / "left.xml"
    right = tmp_path / "right.xml"
    report = tmp_path / "report.json"
    left.write_text(_xml(db="core_nt"), encoding="utf-8")
    right.write_text(_xml(db="16S_ribosomal_RNA"), encoding="utf-8")

    result = _run(left, right, report, "--strict-db")

    assert result.returncode == 1
    payload = json.loads(report.read_text())
    assert payload["differences"][0]["path"] == "provenance.BlastOutput_db_normalized"
