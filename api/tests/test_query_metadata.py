from __future__ import annotations

import pytest

from api.services.query_metadata import parse_fasta_metadata


def test_parse_single_query_fasta_metadata() -> None:
    meta = parse_fasta_metadata(">query1 description\nACGT\nAC GT\n")
    assert meta.query_count == 1
    assert meta.total_letters == 8
    assert meta.min_length == 8
    assert meta.max_length == 8
    assert meta.mixed_lengths is False
    assert meta.records[0].query_id == "query1"
    assert meta.records[0].full_header == "query1 description"
    assert meta.records[0].as_fasta() == ">query1 description\nACGT\nAC GT\n"
    assert meta.as_dict()["records"] == [
        {"query_id": "query1", "length": 8, "full_header": "query1 description"}
    ]


def test_parse_multi_query_mixed_lengths() -> None:
    meta = parse_fasta_metadata(">q1\nAAAA\n>q2\nAA\n")
    assert meta.query_count == 2
    assert meta.total_letters == 6
    assert meta.min_length == 2
    assert meta.max_length == 4
    assert meta.mixed_lengths is True


def test_reject_sequence_before_header() -> None:
    with pytest.raises(ValueError, match="before the first header"):
        parse_fasta_metadata("ACGT\n")


def test_reject_empty_record() -> None:
    with pytest.raises(ValueError, match="no sequence letters"):
        parse_fasta_metadata(">q1\n>q2\nAC\n")


def test_reject_duplicate_query_ids() -> None:
    with pytest.raises(ValueError, match="duplicate query ID"):
        parse_fasta_metadata(">q1 description one\nAC\n>q1 description two\nGT\n")


def test_record_limit() -> None:
    with pytest.raises(ValueError, match="more than 1"):
        parse_fasta_metadata(">q1\nA\n>q2\nA\n", max_records=1)


def test_total_letters_limit() -> None:
    with pytest.raises(ValueError, match="sequence letters"):
        parse_fasta_metadata(">q1\nAAAA\n", max_total_letters=3)