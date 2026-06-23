"""Tests for Sharding Precision behavior.

Responsibility: Tests for Sharding Precision behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_default_sharding_mode_is_off`,
`test_legacy_approximate_flag_maps_to_approximate`,
`test_legacy_explicit_partitions_map_to_approximate`,
`test_explicit_off_conflicts_with_explicit_partitions`, `test_invalid_sharding_mode_rejected`,
`test_off_mode_reports_full_precision`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_sharding_precision.py`.
"""

from __future__ import annotations

import pytest
from api.services.sharding_precision import (
    build_precision_report,
    enrich_tabular_outfmt,
    merge_format_for_outfmt,
    normalize_sharding_mode,
    outfmt_spec_value,
)

_PARITY = "staxids sscinames stitle qcovs"


def test_enrich_bare_tabular_adds_std_and_parity() -> None:
    assert enrich_tabular_outfmt("7") == f"7 std {_PARITY}"
    assert enrich_tabular_outfmt("6") == f"6 std {_PARITY}"
    assert enrich_tabular_outfmt("7 std") == f"7 std {_PARITY}"


def test_enrich_preserves_user_columns() -> None:
    out = enrich_tabular_outfmt("7 qseqid sseqid pident evalue bitscore")
    assert out == f"7 qseqid sseqid pident evalue bitscore {_PARITY}"


def test_enrich_does_not_duplicate_present_columns() -> None:
    # stitle already present → only the other three parity columns are appended.
    assert enrich_tabular_outfmt("7 std stitle") == "7 std stitle staxids sscinames qcovs"


def test_enrich_is_idempotent() -> None:
    once = enrich_tabular_outfmt("7")
    assert enrich_tabular_outfmt(once) == once
    # A layout that already has every parity column is returned untouched.
    full = f"7 std {_PARITY}"
    assert enrich_tabular_outfmt(full) == full


@pytest.mark.parametrize("value", [None, "", "5", "5 ", "  "])
def test_enrich_noop_for_xml_or_empty(value: object) -> None:
    assert enrich_tabular_outfmt(value) == value


def test_enrich_keeps_merge_compatible() -> None:
    # Every enriched tabular layout still passes the shard-merge gate.
    for spec in ("7", "6", "7 std", "7 qseqid sseqid pident evalue bitscore"):
        assert merge_format_for_outfmt(enrich_tabular_outfmt(spec)) == "tabular"


def test_default_sharding_mode_is_off() -> None:
    assert normalize_sharding_mode({}) == "off"


def test_legacy_approximate_flag_maps_to_approximate() -> None:
    assert normalize_sharding_mode({"allow_approximate_sharding": True}) == "approximate"


def test_legacy_explicit_partitions_map_to_approximate() -> None:
    assert normalize_sharding_mode({"db_partitions": 4}) == "approximate"


def test_explicit_off_conflicts_with_explicit_partitions() -> None:
    with pytest.raises(ValueError, match="conflicts"):
        normalize_sharding_mode({"sharding_mode": "off", "db_partitions": 4})


def test_invalid_sharding_mode_rejected() -> None:
    with pytest.raises(ValueError, match="sharding_mode"):
        normalize_sharding_mode({"sharding_mode": "fast"})


def test_off_mode_reports_full_precision() -> None:
    report = build_precision_report({"sharding_mode": "off"})
    assert report.eligible is True
    assert report.precision_level == "full"
    assert report.merge_strategy == "none"


def test_approximate_mode_is_eligible_with_warning() -> None:
    report = build_precision_report(
        {"sharding_mode": "approximate", "outfmt": 6, "db_total_letters": 1000},
        shard_sets=[2],
    )
    assert report.eligible is True
    assert report.precision_level == "approximate"
    assert report.warnings


def test_sharded_xml_outfmt_is_eligible() -> None:
    report = build_precision_report({"sharding_mode": "approximate", "outfmt": 5})
    assert report.eligible is True
    assert report.merge_strategy == "xml_top_n"


def test_additional_outfmt_equals_xml_is_eligible() -> None:
    report = build_precision_report(
        {"sharding_mode": "approximate", "additional_options": "-outfmt=5"}
    )
    assert report.eligible is True
    assert report.merge_strategy == "xml_top_n"


def test_sharded_outfmt7_is_supported() -> None:
    """outfmt 7 shares outfmt 6's tabular layout, so it is merge-eligible."""
    report = build_precision_report({"sharding_mode": "approximate", "outfmt": 7})
    assert report.eligible is True
    assert report.precision_level != "blocked"


def test_sharded_unsupported_outfmt_is_blocked() -> None:
    report = build_precision_report({"sharding_mode": "approximate", "outfmt": 11})
    assert report.eligible is False
    assert report.precision_level == "blocked"
    assert "outfmt 5" in report.blocking_errors[0]


def test_additional_outfmt_equals_unsupported_is_blocked() -> None:
    report = build_precision_report(
        {"sharding_mode": "approximate", "additional_options": "-outfmt=11"}
    )
    assert report.eligible is False
    assert report.precision_level == "blocked"
    assert "outfmt 5" in report.blocking_errors[0]


def test_merge_format_accepts_extended_non_std_layout_with_evalue_bitscore() -> None:
    """An extended, non-std-leading tabular layout that carries evalue + bitscore
    is merge-compatible (the merge resolves columns by name). #29 #2."""
    assert (
        merge_format_for_outfmt("7 qseqid sseqid staxids sstrand pident evalue bitscore")
        == "tabular"
    )
    assert merge_format_for_outfmt("7 std staxids sscinames") == "tabular"
    assert merge_format_for_outfmt("6 std qlen") == "tabular"


def test_merge_format_blocks_layout_missing_rank_columns() -> None:
    """A tabular layout missing evalue or bitscore cannot be re-ranked across
    shards, so it is blocked at submit (mirrors the merge fail-closed)."""
    assert merge_format_for_outfmt("7 qseqid sseqid staxids") is None
    assert merge_format_for_outfmt("7 staxids evalue") is None  # no bitscore


def test_outfmt_spec_value_rejoins_unquoted_multi_token() -> None:
    """`outfmt_spec_value` returns the FULL specifier (not just the leading code)
    so the submit gate sees the whole extended layout."""
    assert (
        outfmt_spec_value("-evalue 0.05 -outfmt 7 std staxids sscinames -dust yes")
        == "7 std staxids sscinames"
    )
    assert outfmt_spec_value('-outfmt "7 qseqid evalue bitscore"') == "7 qseqid evalue bitscore"
    assert outfmt_spec_value("-outfmt 7") == "7"
    assert outfmt_spec_value("-evalue 0.05") is None


def test_precise_single_query_requires_search_space() -> None:
    report = build_precision_report(
        {"sharding_mode": "precise", "outfmt": 6},
        query_count=1,
        db_stats_available=True,
    )
    assert report.eligible is False
    assert report.precision_level == "blocked"
    assert "db_effective_search_space" in report.required_options


def test_precise_mode_requires_query_metadata() -> None:
    report = build_precision_report(
        {"sharding_mode": "precise", "outfmt": 6, "db_effective_search_space": 225},
        query_count=None,
        db_stats_available=True,
    )
    assert report.eligible is False
    assert any("query metadata" in item for item in report.blocking_errors)


def test_precise_single_query_is_eligible_with_search_space() -> None:
    report = build_precision_report(
        {"sharding_mode": "precise", "outfmt": "6 std", "db_effective_search_space": 225},
        query_count="1",  # type: ignore[arg-type]
        db_stats_available=True,
        shard_sets=[2],
    )
    assert report.eligible is True
    assert report.precision_level == "precise_single_query"
    assert report.merge_strategy == "tabular_top_n"


def test_precise_single_query_xml_is_eligible_with_search_space() -> None:
    report = build_precision_report(
        {"sharding_mode": "precise", "outfmt": 5, "db_effective_search_space": 225},
        query_count=1,
        db_stats_available=True,
        shard_sets=[2],
    )
    assert report.eligible is True
    assert report.precision_level == "precise_single_query"
    assert report.merge_strategy == "xml_top_n"


def test_precise_multi_query_is_blocked_until_grouping_lands() -> None:
    report = build_precision_report(
        {"sharding_mode": "precise", "outfmt": 6, "db_effective_search_space": 225},
        query_count=2,
        db_stats_available=True,
    )
    assert report.eligible is False
    assert any("query_effective_search_spaces" in item for item in report.blocking_errors)


def test_precise_multi_query_same_search_space_is_eligible() -> None:
    report = build_precision_report(
        {
            "sharding_mode": "precise",
            "outfmt": 6,
            "query_effective_search_spaces": [225, 225],
        },
        query_count=2,
        db_stats_available=True,
    )
    assert report.eligible is True
    assert report.precision_level == "precise_tabular"


def test_precise_multi_query_mixed_search_spaces_uses_split_strategy() -> None:
    report = build_precision_report(
        {
            "sharding_mode": "precise",
            "outfmt": 6,
            "query_effective_search_spaces": [225, 300],
        },
        query_count=2,
        db_stats_available=True,
    )
    assert report.eligible is True
    assert report.precision_level == "precise_tabular_split"
    assert report.merge_strategy == "query_group_split_tabular_top_n"
    assert report.required_options == {
        "query_split": "one split child job per effective search-space group"
    }


def test_precise_xml_multi_query_mixed_search_spaces_uses_split_strategy() -> None:
    report = build_precision_report(
        {
            "sharding_mode": "precise",
            "outfmt": 5,
            "query_effective_search_spaces": [225, 300],
        },
        query_count=2,
        db_stats_available=True,
    )
    assert report.eligible is True
    assert report.precision_level == "precise_xml_split"
    assert report.merge_strategy == "query_group_split_xml_top_n"


def test_precise_multi_query_rejects_mapping_search_spaces() -> None:
    report = build_precision_report(
        {
            "sharding_mode": "precise",
            "outfmt": 6,
            "query_effective_search_spaces": {"q1": 225, "q2": 225},
        },
        query_count=2,
        db_stats_available=True,
    )
    assert report.eligible is False
    assert any("list ordered" in item for item in report.blocking_errors)


def test_db_effective_search_space_alone_is_single_query_only() -> None:
    report = build_precision_report(
        {
            "sharding_mode": "precise",
            "outfmt": 6,
            "db_effective_search_space": 225,
        },
        query_count=2,
        db_stats_available=True,
    )
    assert report.eligible is False
    assert any("single-query only" in item for item in report.blocking_errors)


def test_precise_multi_query_search_space_count_must_match() -> None:
    report = build_precision_report(
        {
            "sharding_mode": "precise",
            "outfmt": 6,
            "query_effective_search_spaces": [225],
        },
        query_count=2,
        db_stats_available=True,
    )
    assert report.eligible is False
    assert any("count" in item for item in report.blocking_errors)


def test_db_and_query_search_space_conflict_is_blocked() -> None:
    report = build_precision_report(
        {
            "sharding_mode": "precise",
            "outfmt": 6,
            "db_effective_search_space": 225,
            "query_effective_search_spaces": [300, 300],
        },
        query_count=2,
        db_stats_available=True,
    )
    assert report.eligible is False
    assert any("conflicts" in item for item in report.blocking_errors)
