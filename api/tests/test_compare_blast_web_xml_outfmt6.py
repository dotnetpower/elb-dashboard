from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "dev"
    / "compare-blast-web-xml-outfmt6.py"
)


def _run(web_xml: Path, candidate: Path, report: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- test executes checked-in dev script.
        [
            sys.executable,
            str(SCRIPT),
            "--web-xml",
            str(web_xml),
            "--candidate",
            str(candidate),
            "--json",
            str(report),
        ],
        check=False,
        text=True,
        capture_output=True,
    )


def test_compare_web_xml_to_outfmt6_accepts_equivalent_rows(tmp_path: Path) -> None:
    web_xml = tmp_path / "web.xml"
    candidate = tmp_path / "candidate.out"
    report = tmp_path / "report.json"
    web_xml.write_text(
        """<?xml version="1.0"?>
<BlastOutput>
  <BlastOutput_iterations>
    <Iteration>
      <Iteration_query-ID>query</Iteration_query-ID>
      <Iteration_hits>
        <Hit>
          <Hit_id>gi|1|gb|ABC123.1|</Hit_id>
          <Hit_accession>ABC123</Hit_accession>
          <Hit_hsps><Hsp>
            <Hsp_identity>462</Hsp_identity><Hsp_align-len>462</Hsp_align-len>
            <Hsp_gaps>0</Hsp_gaps><Hsp_query-from>1</Hsp_query-from>
            <Hsp_query-to>462</Hsp_query-to><Hsp_hit-from>10</Hsp_hit-from>
            <Hsp_hit-to>471</Hsp_hit-to><Hsp_evalue>0.0</Hsp_evalue>
            <Hsp_bit-score>828.419</Hsp_bit-score><Hsp_score>448</Hsp_score>
          </Hsp></Hit_hsps>
        </Hit>
      </Iteration_hits>
    </Iteration>
  </BlastOutput_iterations>
</BlastOutput>
""",
        encoding="utf-8",
    )
    candidate.write_text(
        "query\tABC123.1\t100.000\t462\t0\t0\t1\t462\t10\t471\t0.0\t828.419\n",
        encoding="utf-8",
    )

    result = _run(web_xml, candidate, report)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(report.read_text())
    assert payload["equivalent"] is True
    assert payload["shared_accessions"] == 1
    assert payload["value_mismatch_count"] == 0


def test_compare_web_xml_to_outfmt6_reports_value_mismatch(tmp_path: Path) -> None:
    web_xml = tmp_path / "web.xml"
    candidate = tmp_path / "candidate.out"
    report = tmp_path / "report.json"
    web_xml.write_text(
        """<?xml version="1.0"?>
<BlastOutput>
  <BlastOutput_iterations>
    <Iteration>
      <Iteration_query-ID>query</Iteration_query-ID>
      <Iteration_hits>
        <Hit>
          <Hit_id>gi|1|gb|ABC123.1|</Hit_id>
          <Hit_accession>ABC123</Hit_accession>
          <Hit_hsps><Hsp>
            <Hsp_identity>462</Hsp_identity><Hsp_align-len>462</Hsp_align-len>
            <Hsp_gaps>0</Hsp_gaps><Hsp_query-from>1</Hsp_query-from>
            <Hsp_query-to>462</Hsp_query-to><Hsp_hit-from>10</Hsp_hit-from>
            <Hsp_hit-to>471</Hsp_hit-to><Hsp_evalue>0.0</Hsp_evalue>
            <Hsp_bit-score>828.419</Hsp_bit-score><Hsp_score>448</Hsp_score>
          </Hsp></Hit_hsps>
        </Hit>
      </Iteration_hits>
    </Iteration>
  </BlastOutput_iterations>
</BlastOutput>
""",
        encoding="utf-8",
    )
    candidate.write_text(
        "query\tABC123.1\t100.000\t462\t0\t0\t1\t462\t10\t471\t0.0\t854\n",
        encoding="utf-8",
    )

    result = _run(web_xml, candidate, report)

    assert result.returncode == 1
    payload = json.loads(report.read_text())
    assert payload["equivalent"] is False
    assert payload["shared_accessions"] == 1
    assert payload["value_mismatch_count"] == 1
    assert payload["first_10_value_mismatches"][0]["differences"]["bits"] == {
        "web": "828.419",
        "candidate": "854",
    }


def test_compare_web_xml_to_outfmt6_uses_optional_raw_score_for_rounded_bits(
    tmp_path: Path,
) -> None:
    web_xml = tmp_path / "web.xml"
    candidate = tmp_path / "candidate.out"
    report = tmp_path / "report.json"
    web_xml.write_text(
        """<?xml version="1.0"?>
<BlastOutput>
  <BlastOutput_iterations>
    <Iteration>
      <Iteration_query-ID>query</Iteration_query-ID>
      <Iteration_hits>
        <Hit>
          <Hit_id>gi|1|gb|ABC123.1|</Hit_id>
          <Hit_accession>ABC123</Hit_accession>
          <Hit_hsps><Hsp>
            <Hsp_identity>462</Hsp_identity><Hsp_align-len>462</Hsp_align-len>
            <Hsp_gaps>0</Hsp_gaps><Hsp_query-from>1</Hsp_query-from>
            <Hsp_query-to>462</Hsp_query-to><Hsp_hit-from>10</Hsp_hit-from>
            <Hsp_hit-to>471</Hsp_hit-to><Hsp_evalue>0.0</Hsp_evalue>
            <Hsp_bit-score>828.419</Hsp_bit-score><Hsp_score>448</Hsp_score>
          </Hsp></Hit_hsps>
        </Hit>
      </Iteration_hits>
    </Iteration>
  </BlastOutput_iterations>
</BlastOutput>
""",
        encoding="utf-8",
    )
    candidate.write_text(
        "query\tABC123.1\t100.000\t462\t0\t0\t1\t462\t10\t471\t0.0\t828\t448\n",
        encoding="utf-8",
    )

    result = _run(web_xml, candidate, report)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(report.read_text())
    assert payload["equivalent"] is True
    assert payload["value_mismatch_count"] == 0