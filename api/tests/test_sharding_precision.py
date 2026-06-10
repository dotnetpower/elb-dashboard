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
from api.services.sharding_precision import build_precision_report, normalize_sharding_mode


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
