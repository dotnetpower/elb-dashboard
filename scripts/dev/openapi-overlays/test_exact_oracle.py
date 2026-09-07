"""Tests for the exact DB-order oracle OpenAPI overlay.

Responsibility: Verify generation/part validation, private manifest upload, and
raw-score outfmt enrichment without network or Azure credentials.
Edit boundaries: Test-only; keep fakes aligned with ``exact_oracle.py``.
Key entry points: pytest test functions.
Risky contracts: Test failures must prove no partial or cross-generation oracle
can be attached and OAuth tokens never appear in returned metadata.
Validation: ``uv run pytest -q scripts/dev/openapi-overlays/test_exact_oracle.py``.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import exact_oracle


def _test_credential() -> str:
    return "".join(("unit", "-", "test", "-", "credential"))


def test_calibration_constants_match_dashboard_policy() -> None:
    from api.services.web_blast_searchsp import (
        CALIBRATION_LENGTH_ADJUSTMENT,
        CALIBRATION_QUERY_LEN,
    )

    assert exact_oracle._CALIBRATION_QUERY_LEN == CALIBRATION_QUERY_LEN
    assert exact_oracle._CALIBRATION_LENGTH_ADJUSTMENT == CALIBRATION_LENGTH_ADJUSTMENT


class _Response:
    def __init__(
        self,
        status_code: int = 200,
        *,
        payload=None,
        size: int = 10,
        content: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = {"Content-Length": str(size)}

    def json(self):
        return self._payload


def _ready_status() -> dict[str, object]:
    return {
        "status": "ready",
        "oracle_format_version": 2,
        "identity": "oracle-v2:layout-1",
        "run_id": "run-1",
        "source_version": "2026-09-01",
        "expected_parts": 2,
        "ready_parts": 2,
        "expected_shards": ["00", "01"],
    }


def test_attach_validates_every_part_and_uploads_private_manifest(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    uploaded: list[dict[str, object]] = []
    credential = _test_credential()
    monkeypatch.setattr(
        exact_oracle.requests,
        "get",
        lambda url, **_kwargs: calls.append(("GET", url)) or _Response(payload=_ready_status()),
    )
    monkeypatch.setattr(
        exact_oracle.requests,
        "head",
        lambda url, **_kwargs: calls.append(("HEAD", url)) or _Response(size=100),
    )
    monkeypatch.setattr(
        exact_oracle.requests,
        "put",
        lambda url, **kwargs: uploaded.append({"url": url, **kwargs}) or _Response(201),
    )

    attached = exact_oracle.attach_db_order_oracle(
        blob_base="https://acct.blob.core.windows.net",
        results_url="https://acct.blob.core.windows.net/results/job-1",
        db_name="core_nt",
        expected_source_version="2026-09-01",
        token=credential,
    )

    assert attached.run_id == "run-1"
    assert attached.part_count == 2
    assert [method for method, _url in calls] == ["GET", "HEAD", "HEAD"]
    manifest = uploaded[0]["data"].decode("utf-8")
    assert manifest.endswith(
        "00.txt\nhttps://acct.blob.core.windows.net/blast-db/metadata/oracles/core_nt/parts/run-1/01.txt\n"
    )
    assert uploaded[0]["headers"]["x-ms-blob-type"] == "BlockBlob"
    assert credential not in str(attached.as_dict())


@pytest.mark.parametrize(
    "mutation",
    [
        {"status": "running"},
        {"source_version": "old"},
        {"ready_parts": 1},
        {"expected_shards": ["00", "00"]},
        {"oracle_format_version": 1},
        {"identity": "oracle-v1:layout-1"},
    ],
)
def test_attach_rejects_incomplete_or_wrong_generation(monkeypatch, mutation) -> None:
    payload = {**_ready_status(), **mutation}
    monkeypatch.setattr(
        exact_oracle.requests,
        "get",
        lambda *_args, **_kwargs: _Response(payload=payload),
    )
    monkeypatch.setattr(
        exact_oracle.requests,
        "head",
        lambda *_args, **_kwargs: pytest.fail("parts must not be checked"),
    )

    with pytest.raises(exact_oracle.ExactOracleUnavailable):
        exact_oracle.attach_db_order_oracle(
            blob_base="https://acct.blob.core.windows.net",
            results_url="https://acct.blob.core.windows.net/results/job-1",
            db_name="core_nt",
            expected_source_version="2026-09-01",
            token=_test_credential(),
        )


def test_attach_rejects_missing_or_empty_part(monkeypatch) -> None:
    monkeypatch.setattr(
        exact_oracle.requests,
        "get",
        lambda *_args, **_kwargs: _Response(payload=_ready_status()),
    )
    monkeypatch.setattr(
        exact_oracle.requests,
        "head",
        lambda *_args, **_kwargs: _Response(size=0),
    )
    put = SimpleNamespace(called=False)
    monkeypatch.setattr(
        exact_oracle.requests,
        "put",
        lambda *_args, **_kwargs: setattr(put, "called", True),
    )

    with pytest.raises(exact_oracle.ExactOracleUnavailable, match="empty part"):
        exact_oracle.attach_db_order_oracle(
            blob_base="https://acct.blob.core.windows.net",
            results_url="https://acct.blob.core.windows.net/results/job-1",
            db_name="core_nt",
            expected_source_version="2026-09-01",
            token=_test_credential(),
        )
    assert put.called is False


def test_read_active_database_uses_generation_identity_and_counts(monkeypatch) -> None:
    monkeypatch.setattr(
        exact_oracle.requests,
        "get",
        lambda url, **_kwargs: _Response(
            payload={
                "source_version": "stale-fallback",
                "active_generation": {
                    "id": "ncbi-direct-20260819-cab30d18c360",
                    "prefix": ("core_nt/generations/ncbi-direct-20260819-cab30d18c360/core_nt"),
                },
                "active_prefix": ("core_nt/generations/ncbi-direct-20260819-cab30d18c360/core_nt"),
                "shard_layout_prefix": (
                    "core_nt/generations/ncbi-direct-20260819-cab30d18c360/shards"
                ),
                "total_letters": 998_069_435_926,
                "total_sequences": 130_155_243,
                "total_bytes": 295_616_990_673,
            }
        ),
    )

    active = exact_oracle.read_active_database(
        blob_base="https://acct.blob.core.windows.net",
        db_name="core_nt",
        token=_test_credential(),
    )

    assert active.source_version == "ncbi-direct-20260819-cab30d18c360"
    assert active.db_prefix.endswith("/ncbi-direct-20260819-cab30d18c360/core_nt")
    assert active.shard_layout_prefix.endswith("/ncbi-direct-20260819-cab30d18c360/shards")
    assert active.total_letters == 998_069_435_926
    assert active.total_sequences == 130_155_243
    assert active.total_bytes == 295_616_990_673
    assert active.search_space == 30_807_003_700_117


def test_read_one_shard_layout_validates_active_generation(monkeypatch) -> None:
    manifest = b"core_nt.00\ncore_nt.01\n"
    nal = b"TITLE core_nt_shard_00\nDBLIST core_nt.00 core_nt.01\n"
    layout_sha = exact_oracle.hashlib.sha256(manifest + b"\0" + nal).hexdigest()
    responses = iter(
        (
            _Response(content=manifest),
            _Response(content=nal),
            _Response(content=f"{layout_sha} 1995000\n".encode()),
            _Response(content=b"DBLIST core_nt.00 core_nt.01\n"),
        )
    )
    monkeypatch.setattr(exact_oracle.requests, "get", lambda *_a, **_k: next(responses))
    active = exact_oracle.ActiveDatabase(
        source_version="generation-1",
        db_prefix="core_nt/generations/generation-1/core_nt",
        shard_layout_prefix="core_nt/generations/generation-1/shards",
        total_letters=1_000_000,
        total_sequences=100,
        search_space=123,
        total_bytes=2_000_000,
    )

    layout = exact_oracle.read_one_shard_layout(
        blob_base="https://acct.blob.core.windows.net",
        db_name="core_nt",
        active_database=active,
        token=_test_credential(),
    )

    assert layout.volume_count == 2
    assert layout.required_bytes == 1_995_000
    assert layout.layout_sha256 == layout_sha
    assert layout.as_dict()["shard_layout_source"] == "active_generation"


@pytest.mark.parametrize("failure", ["gap", "digest", "manifest", "shard_nal", "active_nal"])
def test_read_one_shard_layout_rejects_incomplete_or_tampered_layout(
    monkeypatch,
    failure: str,
) -> None:
    manifest = b"core_nt.00\ncore_nt.02\n" if failure == "manifest" else b"core_nt.00\n"
    nal = b"DBLIST core_nt.01\n" if failure == "shard_nal" else b"DBLIST core_nt.00\n"
    active_nal = (
        b"DBLIST core_nt.00 core_nt.01\n" if failure == "active_nal" else b"DBLIST core_nt.00\n"
    )
    digest = exact_oracle.hashlib.sha256(manifest + b"\0" + nal).hexdigest()
    if failure == "digest":
        digest = "0" * 64
    required_bytes = 800_000_000 if failure == "gap" else 1_995_000_000
    responses = iter(
        (
            _Response(content=manifest),
            _Response(content=nal),
            _Response(content=f"{digest} {required_bytes}\n".encode()),
            _Response(content=active_nal),
        )
    )
    monkeypatch.setattr(exact_oracle.requests, "get", lambda *_a, **_k: next(responses))
    active = exact_oracle.ActiveDatabase(
        source_version="generation-1",
        db_prefix="core_nt/generations/generation-1/core_nt",
        shard_layout_prefix="core_nt/generations/generation-1/shards",
        total_letters=1_000_000,
        total_sequences=100,
        search_space=123,
        total_bytes=2_000_000_000,
    )

    with pytest.raises(exact_oracle.ExactOracleUnavailable):
        exact_oracle.read_one_shard_layout(
            blob_base="https://acct.blob.core.windows.net",
            db_name="core_nt",
            active_database=active,
            token=_test_credential(),
        )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"active_generation": {"id": "generation"}},
        {"total_letters": 1000, "total_sequences": 10},
        {
            "source_version": "generation",
            "active_prefix": "core_nt/core_nt",
            "shard_layout_prefix": "10shards",
            "total_letters": 1_000_000,
            "total_sequences": 10,
        },
    ],
)
def test_read_active_database_fails_closed_on_incomplete_metadata(
    monkeypatch,
    payload,
) -> None:
    monkeypatch.setattr(
        exact_oracle.requests,
        "get",
        lambda *_args, **_kwargs: _Response(payload=payload),
    )

    with pytest.raises(exact_oracle.ExactOracleUnavailable):
        exact_oracle.read_active_database(
            blob_base="https://acct.blob.core.windows.net",
            db_name="core_nt",
            token=_test_credential(),
        )


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ("-evalue 1e-5 -outfmt 5", "-evalue 1e-5 -outfmt 5"),
        ("-evalue 1e-5 -outfmt 6", "-evalue 1e-5 -outfmt 6 std score"),
        ("-outfmt '7 std staxids' -dust yes", "-outfmt 7 std staxids score -dust yes"),
        ("-outfmt 6 std score", "-outfmt 6 std score"),
        ("-evalue 1e-5", "-evalue 1e-5 -outfmt 6 std score"),
    ],
)
def test_ensure_tabular_raw_score(options: str, expected: str) -> None:
    assert exact_oracle.ensure_tabular_raw_score(options) == expected


def test_search_space_from_db_version_uses_active_counts() -> None:
    assert (
        exact_oracle.search_space_from_db_version(
            {
                "detail": {
                    "number_of_letters": "998069435926",
                    "number_of_sequences": "130155243",
                }
            }
        )
        == 30_807_003_700_117
    )


@pytest.mark.parametrize("db_version", [{}, {"detail": {}}, {"detail": "invalid"}])
def test_search_space_from_db_version_fails_closed(db_version) -> None:
    with pytest.raises(exact_oracle.ExactOracleUnavailable):
        exact_oracle.search_space_from_db_version(db_version)


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ("-word_size 28", "-word_size 28 -searchsp 30807003700117"),
        (
            "-searchsp 32156241807668 -dust yes",
            "-dust yes -searchsp 30807003700117",
        ),
        (
            "-searchsp=32156241807668 -outfmt '7 std score'",
            "-outfmt '7 std score' -searchsp 30807003700117",
        ),
        (
            "-dbsize 998069435926 -dust yes",
            "-dust yes -searchsp 30807003700117",
        ),
    ],
)
def test_set_search_space_replaces_stale_forms(options: str, expected: str) -> None:
    assert exact_oracle.set_search_space(options, 30_807_003_700_117) == expected


@pytest.mark.parametrize(
    "options",
    ["-searchsp", "-searchsp -dust yes", "-dbsize", "-dbsize -dust yes"],
)
def test_set_search_space_rejects_missing_scalar(options: str) -> None:
    with pytest.raises(exact_oracle.ExactOracleUnavailable, match="requires a scalar value"):
        exact_oracle.set_search_space(options, 30_807_003_700_117)


def test_preserve_or_set_search_space_keeps_query_specific_value() -> None:
    query_value = 421_817_959_873_974

    result = exact_oracle.preserve_or_set_search_space(
        f"-outfmt 5 -searchsp {query_value} -dust yes",
        30_807_003_700_117,
    )

    assert result == f"-outfmt 5 -searchsp {query_value} -dust yes"


def test_validate_web_blast_execution_options_returns_runtime_evidence() -> None:
    evidence = exact_oracle.validate_web_blast_execution_options(
        "-outfmt 5 -word_size 28 -dust yes -soft_masking false -evalue 0.05 "
        "-max_target_seqs 500 -negative_taxids 3418604,32630 "
        "-dbsize 756264949991 -searchsp 16070630187308750",
        program="blastn",
    )

    assert evidence == {
        "filter_semantics": "blast_taxonomy_filter",
        "filter_mode": "exclude",
        "filter_taxids": [3418604, 32630],
        "candidate_budget": 500,
        "candidate_budget_verified": True,
        "scoring_profile": "megablast_web_default",
        "scoring_profile_verified": True,
    }


@pytest.mark.parametrize(
    "replacement",
    [
        "-word_size 24",
        "-dust no",
        "-soft_masking true",
        "-evalue 1",
        "-max_target_seqs 1000",
        "-outfmt 7",
        "-negative_taxids 0",
        "-negative_taxids 5833 -taxids 10244",
    ],
)
def test_validate_web_blast_execution_options_rejects_inexact_profile(
    replacement: str,
) -> None:
    options = (
        "-outfmt 5 -word_size 28 -dust yes -soft_masking false -evalue 0.05 "
        "-max_target_seqs 500 -negative_taxids 5833"
    )
    option = replacement.split()[0]
    tokens = shlex.split(options)
    index = tokens.index(option)
    end = index + 2
    if replacement == "-negative_taxids 5833 -taxids 10244":
        mutated = f"{options} -taxids 10244"
    else:
        mutated = shlex.join([*tokens[:index], *shlex.split(replacement), *tokens[end:]])

    with pytest.raises(exact_oracle.ExactOracleUnavailable):
        exact_oracle.validate_web_blast_execution_options(mutated, program="blastn")


def test_validate_web_blast_execution_options_rejects_non_blastn_program() -> None:
    with pytest.raises(exact_oracle.ExactOracleUnavailable, match="requires blastn"):
        exact_oracle.validate_web_blast_execution_options(
            "-outfmt 5 -word_size 28 -dust yes -soft_masking false -evalue 0.05 "
            "-max_target_seqs 500 -negative_taxids 5833",
            program="blastx",
        )


def test_preserve_or_set_search_space_uses_active_fallback_when_absent() -> None:
    assert (
        exact_oracle.preserve_or_set_search_space(
            "-outfmt 5 -dust yes",
            30_807_003_700_117,
        )
        == "-outfmt 5 -dust yes -searchsp 30807003700117"
    )
    assert (
        exact_oracle.preserve_or_set_search_space(
            "-outfmt 5 -dbsize 1 -dust yes",
            30_807_003_700_117,
        )
        == "-outfmt 5 -dust yes -searchsp 30807003700117"
    )


@pytest.mark.parametrize(
    "options",
    [
        "-searchsp",
        "-searchsp nope",
        "-searchsp 0",
        "-searchsp 1 -searchsp 2",
        "-searchsp=1 -searchsp 2",
    ],
)
def test_preserve_or_set_search_space_rejects_ambiguous_values(options: str) -> None:
    with pytest.raises(exact_oracle.ExactOracleUnavailable):
        exact_oracle.preserve_or_set_search_space(options, 30_807_003_700_117)


def test_prepare_web_blast_statistics_canonicalizes_runtime_options() -> None:
    active = exact_oracle.ActiveDatabase(
        source_version="ncbi-direct-20260819-cab30d18c360",
        db_prefix="core_nt/generations/ncbi-direct-20260819-cab30d18c360/core_nt",
        shard_layout_prefix="core_nt/generations/ncbi-direct-20260819-cab30d18c360/shards",
        total_letters=998_069_435_926,
        total_sequences=130_155_243,
        search_space=30_807_003_700_117,
    )
    context = {
        "filtered_database_letters": 994_867_281_343,
        "filtered_database_sequences": 130_118_804,
        "length_adjustment": 36,
        "effective_search_space": 421_817_959_873_974,
        "scoring_search_space": 423_813_461_852_118,
        "result_database_letters": 998_069_435_926,
    }

    options, statistics = exact_oracle.prepare_web_blast_statistics(
        context=context,
        query_fasta=">query-1 description\n" + "A" * 462,
        active_database=active,
        options="-outfmt 5 -dbsize 1 -searchsp 2 -dust yes",
    )

    assert options == ("-outfmt 5 -dust yes -dbsize 994867281343 -searchsp 423813461852118")
    assert statistics is not None
    assert statistics.as_dict() == {
        "schema_version": 1,
        "query_id": "query-1",
        "query_length": 462,
        "filtered_database_letters": 994_867_281_343,
        "filtered_database_sequences": 130_118_804,
        "length_adjustment": 36,
        "effective_search_space": 421_817_959_873_974,
        "scoring_search_space": 423_813_461_852_118,
        "result_database_letters": 998_069_435_926,
        "active_database_letters": 998_069_435_926,
        "active_database_sequences": 130_155_243,
        "active_source_version": "ncbi-direct-20260819-cab30d18c360",
    }


def test_web_blast_statistics_select_monolithic_candidate_search() -> None:
    statistics = exact_oracle.WebBlastStatistics(
        query_id="q1",
        query_length=462,
        filtered_database_letters=994_867_281_343,
        filtered_database_sequences=130_118_804,
        length_adjustment=36,
        effective_search_space=421_817_959_873_974,
        scoring_search_space=423_813_461_852_118,
        result_database_letters=998_069_435_926,
        active_database_letters=998_069_435_926,
        active_database_sequences=130_155_243,
        active_source_version="ncbi-direct-20260819-cab30d18c360",
    )

    assert exact_oracle.select_web_blast_partitions(statistics, default_partitions=10) == 1


def test_precise_request_without_web_statistics_keeps_parallel_shards() -> None:
    assert exact_oracle.select_web_blast_partitions(None, default_partitions=10) == 10


@pytest.mark.parametrize("value", [True, 0, -1, exact_oracle._MAX_PARTS + 1])
def test_web_blast_partition_selection_rejects_invalid_default(value: object) -> None:
    with pytest.raises(exact_oracle.ExactOracleUnavailable):
        exact_oracle.select_web_blast_partitions(None, default_partitions=value)  # type: ignore[arg-type]


def test_attach_web_blast_statistics_uploads_private_manifest(monkeypatch) -> None:
    uploaded: list[dict[str, object]] = []
    credential = _test_credential()
    statistics = exact_oracle.WebBlastStatistics(
        query_id="q1",
        query_length=462,
        filtered_database_letters=994_867_281_343,
        filtered_database_sequences=130_118_804,
        length_adjustment=36,
        effective_search_space=421_817_959_873_974,
        scoring_search_space=423_813_461_852_118,
        result_database_letters=998_069_435_926,
        active_database_letters=998_069_435_926,
        active_database_sequences=130_155_243,
        active_source_version="ncbi-direct-20260819-cab30d18c360",
    )
    monkeypatch.setattr(
        exact_oracle.requests,
        "put",
        lambda url, **kwargs: uploaded.append({"url": url, **kwargs}) or _Response(201),
    )

    result = exact_oracle.attach_web_blast_statistics(
        blob_base="https://acct.blob.core.windows.net",
        results_url="https://acct.blob.core.windows.net/results/job-1",
        statistics=statistics,
        token=credential,
    )

    assert result == (
        "https://acct.blob.core.windows.net/results/job-1/metadata/web-blast-statistics.json"
    )
    payload = uploaded[0]["data"].decode("utf-8")
    assert '"scoring_search_space": 423813461852118' in payload
    assert uploaded[0]["headers"]["x-ms-blob-type"] == "BlockBlob"
    assert uploaded[0]["headers"]["If-None-Match"] == "*"
    assert credential not in payload


def test_attach_web_blast_statistics_accepts_only_identical_replay(monkeypatch) -> None:
    statistics = exact_oracle.WebBlastStatistics(
        query_id="q1",
        query_length=10,
        filtered_database_letters=2900,
        filtered_database_sequences=2,
        length_adjustment=1,
        effective_search_space=26082,
        scoring_search_space=26100,
        result_database_letters=3000,
        active_database_letters=3000,
        active_database_sequences=3,
        active_source_version="generation-1",
    )
    expected = (json.dumps(statistics.as_dict(), sort_keys=True) + "\n").encode()
    monkeypatch.setattr(
        exact_oracle.requests,
        "put",
        lambda *_args, **_kwargs: _Response(412),
    )
    monkeypatch.setattr(
        exact_oracle.requests,
        "get",
        lambda *_args, **_kwargs: _Response(200, content=expected),
    )

    exact_oracle.attach_web_blast_statistics(
        blob_base="https://acct.blob.core.windows.net",
        results_url="https://acct.blob.core.windows.net/results/job-1",
        statistics=statistics,
        token=_test_credential(),
    )

    monkeypatch.setattr(
        exact_oracle.requests,
        "get",
        lambda *_args, **_kwargs: _Response(200, content=b"{}\n"),
    )
    with pytest.raises(
        exact_oracle.ExactOracleUnavailable,
        match="changed across an idempotent replay",
    ):
        exact_oracle.attach_web_blast_statistics(
            blob_base="https://acct.blob.core.windows.net",
            results_url="https://acct.blob.core.windows.net/results/job-1",
            statistics=statistics,
            token=_test_credential(),
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("effective_search_space", 1, "effective search space"),
        ("scoring_search_space", 1, "scoring search space"),
        ("filtered_database_letters", 998_069_435_927, "exceed"),
        ("filtered_database_sequences", 130_155_244, "exceed"),
        ("result_database_letters", 1, "result database length"),
    ],
)
def test_prepare_web_blast_statistics_rejects_invalid_context(
    field: str,
    value: int,
    match: str,
) -> None:
    active = exact_oracle.ActiveDatabase(
        source_version="generation",
        db_prefix="core_nt/generations/generation/core_nt",
        shard_layout_prefix="core_nt/generations/generation/shards",
        total_letters=998_069_435_926,
        total_sequences=130_155_243,
        search_space=30_807_003_700_117,
    )
    context = {
        "filtered_database_letters": 994_867_281_343,
        "filtered_database_sequences": 130_118_804,
        "length_adjustment": 36,
        "effective_search_space": 421_817_959_873_974,
        "scoring_search_space": 423_813_461_852_118,
        "result_database_letters": 998_069_435_926,
    }
    context[field] = value

    with pytest.raises(exact_oracle.ExactOracleUnavailable, match=match):
        exact_oracle.prepare_web_blast_statistics(
            context=context,
            query_fasta=">q\n" + "A" * 462,
            active_database=active,
            options="-outfmt 5",
        )
