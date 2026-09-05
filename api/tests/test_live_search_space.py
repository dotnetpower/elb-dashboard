"""Tests for active-generation Web BLAST search-space resolution.

Responsibility: Verify calibrated metadata lookup, scalar overwrite,
query-specific preservation/transport, fail-closed behavior, and scalar CLI
search-space replacement.
Edit boundaries: Test-only; mock Storage metadata and never call Azure.
Key entry points: pytest test functions.
Risky contracts: Approximate/uncalibrated requests remain untouched while
precise calibrated requests never retain stale caller scalar values. Explicit
query-level values survive until the sibling transport boundary, where only a
uniform list can collapse losslessly.
Validation: `uv run pytest -q api/tests/test_live_search_space.py`.
"""

from __future__ import annotations

import pytest
from api.services.blast import live_search_space


@pytest.fixture
def live_metadata(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    metadata: dict[str, object] = {
        "total_letters": 998_069_435_926,
        "total_sequences": 130_155_243,
        "source_version": "stale-fallback",
        "active_generation": {"id": "ncbi-direct-20260819-cab30d18c360"},
    }
    monkeypatch.setattr(
        "api.services.blast.db_metadata.resolve_db_metadata",
        lambda account, db: metadata if account == "workloadstg" and db == "core_nt" else None,
    )
    return metadata


def test_resolve_live_search_space_uses_active_counts(live_metadata) -> None:
    result = live_search_space.resolve_live_search_space(
        "core_nt",
        storage_account="workloadstg",
    )

    assert result is not None
    assert result.value == 30_807_003_700_117
    assert result.total_letters == live_metadata["total_letters"]
    assert result.total_sequences == live_metadata["total_sequences"]
    assert result.source_version == "ncbi-direct-20260819-cab30d18c360"


def test_precise_options_replace_stale_caller_value(live_metadata) -> None:
    result = live_search_space.canonicalize_precise_options(
        "core_nt",
        {
            "sharding_mode": "precise",
            "db_effective_search_space": 32_156_241_807_668,
            "db_total_letters": 1,
            "db_total_sequences": 1,
            "additional_options": "-dbsize 1 -searchsp 32156241807668 -dust yes",
        },
        storage_account="workloadstg",
    )

    assert result["db_effective_search_space"] == 30_807_003_700_117
    assert result["db_total_letters"] == live_metadata["total_letters"]
    assert result["db_total_sequences"] == live_metadata["total_sequences"]
    assert result["additional_options"] == "-dust yes -searchsp 30807003700117"


def test_precise_options_preserve_query_specific_search_space(live_metadata) -> None:
    query_search_space = 421_817_959_873_974

    result = live_search_space.canonicalize_precise_options(
        "core_nt",
        {
            "sharding_mode": "precise",
            "db_effective_search_space": 32_156_241_807_668,
            "query_effective_search_spaces": [query_search_space],
            "additional_options": "-negative_taxids 3431483 -dust yes",
        },
        storage_account="workloadstg",
    )

    assert "db_effective_search_space" not in result
    assert result["query_effective_search_spaces"] == [query_search_space]
    assert result["db_total_letters"] == live_metadata["total_letters"]
    assert result["db_total_sequences"] == live_metadata["total_sequences"]
    assert result["additional_options"] == "-negative_taxids 3431483 -dust yes"


def test_uniform_query_search_space_collapses_for_sibling_transport() -> None:
    result = live_search_space.collapse_uniform_query_search_space(
        {
            "query_effective_search_spaces": [123, 123],
            "db_total_letters": 456,
        }
    )

    assert result == {
        "db_effective_search_space": 123,
        "db_total_letters": 456,
    }


def test_web_blast_statistical_context_uses_distinct_scoring_space() -> None:
    context = {
        "filtered_database_letters": 994_867_281_343,
        "filtered_database_sequences": 130_118_804,
        "length_adjustment": 36,
        "effective_search_space": 421_817_959_873_974,
        "scoring_search_space": 423_813_461_852_118,
        "result_database_letters": 998_069_435_926,
    }

    validated = live_search_space.validate_web_blast_statistical_context(
        context,
        query_lengths=[462],
        active_total_letters=998_069_435_926,
        active_total_sequences=130_155_243,
    )
    transported = live_search_space.collapse_uniform_query_search_space(
        {
            "query_effective_search_spaces": [421_817_959_873_974],
            "web_blast_statistical_context": validated,
        }
    )

    assert transported == {
        "db_effective_search_space": 423_813_461_852_118,
        "web_blast_statistical_context": context,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("effective_search_space", 421_817_959_873_975),
        ("scoring_search_space", 423_813_461_852_119),
        ("filtered_database_letters", 998_069_435_927),
        ("filtered_database_sequences", 130_155_244),
        ("result_database_letters", 1),
    ],
)
def test_web_blast_statistical_context_rejects_inconsistent_values(
    field: str,
    value: int,
) -> None:
    context = {
        "filtered_database_letters": 994_867_281_343,
        "filtered_database_sequences": 130_118_804,
        "length_adjustment": 36,
        "effective_search_space": 421_817_959_873_974,
        "scoring_search_space": 423_813_461_852_118,
        "result_database_letters": 998_069_435_926,
    }
    context[field] = value

    with pytest.raises(ValueError, match="web_blast_statistical_context"):
        live_search_space.validate_web_blast_statistical_context(
            context,
            query_lengths=[462],
            active_total_letters=998_069_435_926,
            active_total_sequences=130_155_243,
        )


def test_mixed_query_search_spaces_fail_sibling_transport() -> None:
    with pytest.raises(ValueError, match="require query-group execution"):
        live_search_space.collapse_uniform_query_search_space(
            {"query_effective_search_spaces": [123, 456]}
        )


def test_precise_calibrated_database_fails_closed_without_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "api.services.blast.db_metadata.resolve_db_metadata",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(
        live_search_space.LiveSearchSpaceUnavailable,
        match="active database metadata is unavailable",
    ):
        live_search_space.canonicalize_precise_options(
            "core_nt",
            {"sharding_mode": "precise"},
            storage_account="workloadstg",
        )


def test_approximate_and_uncalibrated_options_do_not_read_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "api.services.blast.db_metadata.resolve_db_metadata",
        lambda *_args, **_kwargs: pytest.fail("metadata lookup must be skipped"),
    )

    assert live_search_space.canonicalize_precise_options(
        "core_nt", {"sharding_mode": "approximate"}, storage_account="workloadstg"
    ) == {"sharding_mode": "approximate"}
    assert live_search_space.canonicalize_precise_options(
        "custom_nt", {"sharding_mode": "precise"}, storage_account="workloadstg"
    ) == {"sharding_mode": "precise"}


@pytest.mark.parametrize(
    ("initial", "expected"),
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
def test_set_search_space_option_replaces_stale_forms(initial: str, expected: str) -> None:
    assert live_search_space.set_search_space_option(initial, 30_807_003_700_117) == expected


@pytest.mark.parametrize(
    "options",
    ["-searchsp", "-searchsp -dust yes", "-dbsize", "-dbsize -dust yes"],
)
def test_set_search_space_option_rejects_missing_scalar(options: str) -> None:
    with pytest.raises(ValueError, match="requires a scalar value"):
        live_search_space.set_search_space_option(options, 30_807_003_700_117)
