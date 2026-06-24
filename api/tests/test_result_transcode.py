"""Tests for on-the-fly result decompression + format transcoding.

Responsibility: lock the pure helpers in
`api/services/blast/result_transcode.py` — streaming/bounded gunzip and the
XML/tabular → csv/tsv/json re-render with its size + parse error contracts.
Edit boundaries: pure-function tests only; route-level wiring is covered in
`test_external_blast_api.py`.
Key entry points: the test functions below.
Risky contracts: gunzip must stay memory-bounded and raise on oversize;
transcode must raise (never silently truncate) on a parse failure.
Validation: `uv run pytest -q api/tests/test_result_transcode.py`.
"""

from __future__ import annotations

import csv
import gzip
import io
import json

import pytest
from api.services.blast import result_transcode as rt

_TABULAR = (
    "# BLASTN 2.17.0+\n"
    "# Query: q1\n"
    "# Fields: query id, subject id, % identity, alignment length, mismatches, "
    "gap opens, q. start, q. end, s. start, s. end, evalue, bit score\n"
    "q1\tNR_1\t99.5\t200\t1\t0\t1\t200\t1\t200\t1e-50\t370\n"
    "q1\tNR_2\t88.0\t150\t10\t1\t1\t150\t1\t150\t1e-20\t180\n"
)

_XML = (
    '<?xml version="1.0"?>\n<BlastOutput>\n<BlastOutput_iterations>\n'
    "<Iteration><Iteration_query-def>q1</Iteration_query-def><Iteration_hits>\n"
    "<Hit><Hit_num>1</Hit_num><Hit_id>NR_1</Hit_id><Hit_def>x</Hit_def>"
    "<Hit_accession>NR_1</Hit_accession><Hit_len>200</Hit_len><Hit_hsps>\n"
    "<Hsp><Hsp_num>1</Hsp_num><Hsp_bit-score>370</Hsp_bit-score><Hsp_score>200</Hsp_score>"
    "<Hsp_evalue>1e-50</Hsp_evalue><Hsp_query-from>1</Hsp_query-from><Hsp_query-to>200</Hsp_query-to>"
    "<Hsp_hit-from>1</Hsp_hit-from><Hsp_hit-to>200</Hsp_hit-to><Hsp_identity>199</Hsp_identity>"
    "<Hsp_align-len>200</Hsp_align-len><Hsp_gaps>0</Hsp_gaps>"
    "<Hsp_qseq>ACGT</Hsp_qseq><Hsp_hseq>ACGT</Hsp_hseq><Hsp_midline>||||</Hsp_midline></Hsp>\n"
    "</Hit_hsps></Hit>\n"
    "</Iteration_hits></Iteration>\n</BlastOutput_iterations>\n</BlastOutput>\n"
)


def test_is_gzip_name_and_strip() -> None:
    assert rt.is_gzip_name("merged_results.out.gz")
    assert not rt.is_gzip_name("merged_results.out")
    assert rt.strip_gzip_suffix("a.out.gz") == "a.out"
    assert rt.strip_gzip_suffix("a.out") == "a.out"


def test_gunzip_stream_roundtrips() -> None:
    raw = _TABULAR.encode("utf-8")
    blob = gzip.compress(raw)
    # Feed the gzip blob in two chunks to exercise the streaming path.
    chunks = [blob[: len(blob) // 2], blob[len(blob) // 2 :]]
    out = b"".join(rt.gunzip_stream(iter(chunks)))
    assert out == raw


def test_gunzip_bytes_roundtrips() -> None:
    raw = _TABULAR.encode("utf-8")
    assert rt.gunzip_bytes(gzip.compress(raw)) == raw


def test_gunzip_bytes_rejects_oversize() -> None:
    raw = b"A" * (4 * 1024 * 1024)
    blob = gzip.compress(raw)
    with pytest.raises(rt.ResultTooLargeError):
        rt.gunzip_bytes(blob, max_output=1024)


def test_gunzip_bytes_rejects_non_gzip() -> None:
    with pytest.raises(rt.ResultParseError):
        rt.gunzip_bytes(b"not gzip at all")


def test_transcode_tabular_to_csv() -> None:
    body, media_type, filename = rt.transcode_result_bytes(
        _TABULAR.encode("utf-8"),
        source_filename="merged_results.out.gz",
        target_format="csv",
    )
    assert media_type == "text/csv"
    assert filename == "merged_results.csv"
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8"))))
    assert len(rows) == 2
    assert rows[0]["sseqid"] == "NR_1"


def test_transcode_tabular_to_tsv_uses_tab() -> None:
    body, media_type, filename = rt.transcode_result_bytes(
        _TABULAR.encode("utf-8"),
        source_filename="m.out",
        target_format="tsv",
    )
    assert media_type == "text/tab-separated-values"
    assert filename == "m.tsv"
    assert "\t" in body.decode("utf-8").splitlines()[0]


def test_transcode_to_json_returns_hits() -> None:
    body, media_type, filename = rt.transcode_result_bytes(
        _TABULAR.encode("utf-8"),
        source_filename="m.out",
        target_format="json",
    )
    assert media_type == "application/json"
    assert filename == "m.json"
    payload = json.loads(body)
    assert payload["total"] == 2
    assert payload["hits"][0]["sseqid"] == "NR_1"


def test_transcode_xml_to_csv() -> None:
    body, _media, filename = rt.transcode_result_bytes(
        _XML.encode("utf-8"),
        source_filename="merged_results.xml",
        target_format="csv",
    )
    assert filename == "merged_results.csv"
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8"))))
    assert len(rows) == 1
    assert rows[0]["sseqid"] == "NR_1"


def test_transcode_rejects_unknown_format() -> None:
    with pytest.raises(rt.ResultTranscodeError):
        rt.transcode_result_bytes(
            _TABULAR.encode("utf-8"), source_filename="m.out", target_format="pdf"
        )


def test_transcode_rejects_oversize_input() -> None:
    big = b"x" * (rt.TRANSCODE_MAX_BYTES + 1)
    with pytest.raises(rt.ResultTooLargeError):
        rt.transcode_result_bytes(big, source_filename="m.out", target_format="json")


def test_result_media_type_for() -> None:
    assert rt.result_media_type_for("a.xml") == "application/xml"
    assert rt.result_media_type_for("a.out") == "text/plain"
    assert rt.result_media_type_for("a.csv") == "text/csv"
    assert rt.result_media_type_for("a.json") == "application/json"
    assert rt.result_media_type_for("a.bin") == "application/octet-stream"
