"""Unit tests for the auto-sharding logic in api.services.blast.config.

Responsibility: Unit tests for the auto-sharding logic in api.services.blast.config
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_parse`, `_base_params`,
`test_auto_sharding_is_off_but_local_ssd_is_on_by_default`,
`test_warmup_skip_option_is_opt_in`,
`test_local_ssd_cannot_be_disabled_while_pv_path_is_paused`,
`test_approximate_sharding_opt_in_injects_partitions_and_prefix`,
`test_approximate_sharding_uses_full_dbsize_when_available`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_blast_config_sharding.py`.
"""

from __future__ import annotations

import configparser
import io

import pytest
from api.services.blast.config import generate_config


def _parse(content: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read_file(io.StringIO(content))
    return parser


def _base_params() -> dict[str, object]:
    return {
        "region": "koreacentral",
        "resource_group": "rg-elb",
        "storage_account": "elbstg01",
        "aks_cluster_name": "elb-cluster",
        "machine_type": "Standard_E16s_v5",
        "num_nodes": 5,
        "program": "blastn",
        "db": "https://elbstg01.blob.core.windows.net/blast-db/core_nt/core_nt",
        "query_blob_url": "https://elbstg01.blob.core.windows.net/queries/q.fa",
        "results_url": "https://elbstg01.blob.core.windows.net/results/job-1",
        "job_id": "job-1",
        # Sharding-eligibility metadata supplied by the route layer.
        "db_name": "core_nt",
        "db_sharded": True,
        "db_total_bytes": 269 * 1024**3,
    }


def test_auto_sharding_is_off_but_local_ssd_is_on_by_default() -> None:
    cfg = _parse(generate_config(_base_params()))
    assert not cfg.has_option("blast", "db-partitions")
    assert not cfg.has_option("blast", "db-partition-prefix")
    assert cfg.get("cluster", "exp-use-local-ssd") == "true"
    assert not cfg.has_option("cluster", "exp-skip-warmed-ssd-init")


def test_warmup_skip_option_is_opt_in() -> None:
    params = _base_params()
    params["skip_warmed_ssd_init"] = True

    cfg = _parse(generate_config(params))

    assert cfg.get("cluster", "exp-skip-warmed-ssd-init") == "true"


def test_generate_config_rejects_storage_account_mismatch() -> None:
    params = _base_params()
    params["db"] = "https://stgelb.blob.core.windows.net/blast-db/core_nt/core_nt"

    with pytest.raises(ValueError, match="db URL must belong"):
        generate_config(params)


def test_generate_config_rejects_query_string_blob_urls() -> None:
    params = _base_params()
    params["query_blob_url"] = "https://elbstg01.blob.core.windows.net/queries/q.fa?sig=bad"

    with pytest.raises(ValueError, match="query_blob_url URL must not include query strings"):
        generate_config(params)


def test_local_ssd_cannot_be_disabled_while_pv_path_is_paused() -> None:
    params = _base_params()
    params["use_local_ssd"] = False
    cfg = _parse(generate_config(params))
    assert cfg.get("cluster", "exp-use-local-ssd") == "true"


def test_approximate_sharding_opt_in_injects_partitions_and_prefix() -> None:
    params = _base_params()
    params["allow_approximate_sharding"] = True
    cfg = _parse(generate_config(params))
    # 269 GB on E16 (128 GB) → memory floor 5; num_nodes=5 → target 5 → preset 5
    assert cfg.get("blast", "db-partitions") == "5"
    assert cfg.get("blast", "db-partition-prefix") == (
        "https://elbstg01.blob.core.windows.net/blast-db/5shards/core_nt_shard_"
    )
    # Sharding requires the local-SSD init script.
    assert cfg.get("cluster", "exp-use-local-ssd") == "true"


def test_approximate_sharding_accepts_outfmt7() -> None:
    """outfmt 7 is a merge-compatible tabular layout, so a sharded config with
    outfmt 7 builds successfully (the elastic-blast runtime gate is widened by
    terminal/patch_elastic_blast.py to match)."""
    params = _base_params()
    params["allow_approximate_sharding"] = True
    params["outfmt"] = 7
    cfg = _parse(generate_config(params))
    assert cfg.get("blast", "db-partitions") == "5"
    assert "-outfmt 7" in cfg.get("blast", "options")



def test_approximate_sharding_uses_full_dbsize_when_available() -> None:
    params = _base_params()
    params["allow_approximate_sharding"] = True
    params["db_total_letters"] = 29_999_612
    cfg = _parse(generate_config(params))
    assert "-dbsize 29999612" in cfg.get("blast", "options")


def test_effective_search_space_overrides_dbsize_for_precise_single_query() -> None:
    params = _base_params()
    params["allow_approximate_sharding"] = True
    params["db_total_letters"] = 29_999_612
    params["db_effective_search_space"] = 2_254_169_736
    cfg = _parse(generate_config(params))
    options = cfg.get("blast", "options")
    assert "-searchsp 2254169736" in options
    assert "-dbsize" not in options


def test_effective_search_space_must_be_positive_integer() -> None:
    params = _base_params()
    params["allow_approximate_sharding"] = True
    params["db_effective_search_space"] = 0
    with pytest.raises(ValueError, match="db_effective_search_space"):
        generate_config(params)


def test_precise_sharding_requires_effective_search_space() -> None:
    params = _base_params()
    params["sharding_mode"] = "precise"
    params["query_count"] = 1
    with pytest.raises(ValueError, match="db_effective_search_space"):
        generate_config(params)


def test_precise_sharding_injects_searchsp() -> None:
    params = _base_params()
    params["sharding_mode"] = "precise"
    params["query_count"] = 1
    params["db_effective_search_space"] = 2_254_169_736
    cfg = _parse(generate_config(params))
    assert cfg.get("blast", "db-partitions") == "5"
    assert "-searchsp 2254169736" in cfg.get("blast", "options")


def test_precise_multi_query_uniform_search_space_injects_searchsp() -> None:
    params = _base_params()
    params["sharding_mode"] = "precise"
    params["query_count"] = 2
    params["query_effective_search_spaces"] = [2_254_169_736, 2_254_169_736]
    cfg = _parse(generate_config(params))
    assert cfg.get("blast", "db-partitions") == "5"
    assert "-searchsp 2254169736" in cfg.get("blast", "options")


def test_precise_multi_query_mixed_search_spaces_rejected() -> None:
    params = _base_params()
    params["sharding_mode"] = "precise"
    params["query_count"] = 2
    params["query_effective_search_spaces"] = [2_254_169_736, 3_000_000_000]
    with pytest.raises(ValueError, match="query-group"):
        generate_config(params)


def test_precise_multi_query_mapping_search_spaces_rejected() -> None:
    params = _base_params()
    params["sharding_mode"] = "precise"
    params["query_count"] = 2
    params["query_effective_search_spaces"] = {"q1": 2_254_169_736, "q2": 2_254_169_736}
    with pytest.raises(ValueError, match="list ordered"):
        generate_config(params)


def test_sharded_merge_allows_xml_outfmt() -> None:
    params = _base_params()
    params["allow_approximate_sharding"] = True
    params["outfmt"] = 5
    cfg = _parse(generate_config(params))
    assert "-outfmt 5" in cfg.get("blast", "options")


def test_sharded_merge_allows_additional_outfmt_equals_xml() -> None:
    params = _base_params()
    params["allow_approximate_sharding"] = True
    params["additional_options"] = "-outfmt=5"
    cfg = _parse(generate_config(params))
    assert "-outfmt=5" in cfg.get("blast", "options")


def test_sharded_merge_rejects_unsupported_outfmt() -> None:
    params = _base_params()
    params["allow_approximate_sharding"] = True
    params["outfmt"] = 11
    with pytest.raises(ValueError, match="outfmt 5"):
        generate_config(params)


def test_sharded_merge_rejects_additional_outfmt_equals_unsupported() -> None:
    params = _base_params()
    params["allow_approximate_sharding"] = True
    params["additional_options"] = "-outfmt=11"
    with pytest.raises(ValueError, match="outfmt 5"):
        generate_config(params)


def test_sharded_merge_allows_tabular_std_outfmt() -> None:
    params = _base_params()
    params["allow_approximate_sharding"] = True
    params["outfmt"] = "6 std qlen"
    cfg = _parse(generate_config(params))
    assert "-outfmt 6 std qlen" in cfg.get("blast", "options")


def test_unsharded_submit_allows_non_tabular_outfmt() -> None:
    params = _base_params()
    params["outfmt"] = 5
    cfg = _parse(generate_config(params))
    assert cfg.get("blast", "options") == "-outfmt 5"


def test_low_complexity_filter_injects_dust_for_blastn() -> None:
    params = _base_params()
    params["low_complexity_filter"] = True
    cfg = _parse(generate_config(params))
    options = cfg.get("blast", "options")
    assert "-dust yes" in options
    assert "-soft_masking false" in options


def test_low_complexity_filter_can_disable_dust_for_blastn() -> None:
    params = _base_params()
    params["low_complexity_filter"] = False
    cfg = _parse(generate_config(params))
    options = cfg.get("blast", "options")
    assert "-dust no" in options
    assert "-soft_masking" not in options


def test_low_complexity_filter_respects_explicit_dust_option() -> None:
    params = _base_params()
    params["low_complexity_filter"] = True
    params["additional_options"] = "-dust no"
    cfg = _parse(generate_config(params))
    options = cfg.get("blast", "options")
    assert options.count("-dust") == 1
    assert "-dust no" in options
    assert "-soft_masking false" in options


def test_low_complexity_filter_respects_explicit_soft_masking_option() -> None:
    params = _base_params()
    params["low_complexity_filter"] = True
    params["additional_options"] = "-soft_masking true"
    cfg = _parse(generate_config(params))
    options = cfg.get("blast", "options")
    assert "-dust yes" in options
    assert options.count("-soft_masking") == 1
    assert "-soft_masking true" in options


def test_taxid_filter_includes_taxonomy_by_default() -> None:
    params = _base_params()
    params["taxid"] = 3431483
    cfg = _parse(generate_config(params))
    options = cfg.get("blast", "options")
    assert "-taxids 3431483" in options
    assert "-negative_taxids" not in options


def test_taxid_filter_excludes_taxonomy_when_not_inclusive() -> None:
    params = _base_params()
    params["taxid"] = "3431483"
    params["is_inclusive"] = False
    cfg = _parse(generate_config(params))
    options = cfg.get("blast", "options")
    assert "-negative_taxids 3431483" in options
    assert "-taxids 3431483" not in options


def test_taxid_filter_rejects_invalid_taxid() -> None:
    params = _base_params()
    params["taxid"] = 0
    with pytest.raises(ValueError, match="taxid must be a positive integer"):
        generate_config(params)


def test_taxid_filter_rejects_bool_taxid() -> None:
    params = _base_params()
    params["taxid"] = True
    with pytest.raises(ValueError, match="taxid must be a positive integer"):
        generate_config(params)


def test_taxid_filter_rejects_ambiguous_inclusive_flag() -> None:
    params = _base_params()
    params["taxid"] = 3431483
    params["is_inclusive"] = "maybe"
    with pytest.raises(ValueError, match="is_inclusive must be a boolean"):
        generate_config(params)


@pytest.mark.parametrize("additional", ["-taxids 2", "-negative_taxids=2"])
def test_taxid_filter_rejects_additional_taxonomy_conflict(additional: str) -> None:
    params = _base_params()
    params["taxid"] = 3431483
    params["additional_options"] = additional
    with pytest.raises(ValueError, match="taxid conflicts"):
        generate_config(params)


def test_auto_sharding_disabled_when_caller_opts_out() -> None:
    params = _base_params()
    params["disable_sharding"] = True
    cfg = _parse(generate_config(params))
    assert not cfg.has_option("blast", "db-partitions")
    assert not cfg.has_option("blast", "db-partition-prefix")
    # Disabling shard partitioning must not re-enable the shared PV/PVC path.
    assert cfg.get("cluster", "exp-use-local-ssd") == "true"


def test_legacy_db_auto_partition_maps_to_approximate_sharding() -> None:
    params = _base_params()
    params["db_auto_partition"] = True
    cfg = _parse(generate_config(params))
    assert cfg.get("blast", "db-partitions") == "5"
    assert not cfg.has_option("blast", "db-auto-partition")


def test_db_auto_partition_rejects_explicit_off_mode() -> None:
    params = _base_params()
    params["db_auto_partition"] = True
    params["sharding_mode"] = "off"
    with pytest.raises(ValueError, match="sharding_mode=approximate"):
        generate_config(params)


def test_db_auto_partition_allowed_with_approximate_sharding_opt_in() -> None:
    params = _base_params()
    params["db_sharded"] = False
    params["db_auto_partition"] = True
    params["allow_approximate_sharding"] = True
    cfg = _parse(generate_config(params))
    assert not cfg.has_option("blast", "db-auto-partition")
    assert not cfg.has_option("blast", "db-partitions")


def test_auto_sharding_skipped_when_db_not_sharded() -> None:
    params = _base_params()
    params["db_sharded"] = False
    cfg = _parse(generate_config(params))
    assert not cfg.has_option("blast", "db-partitions")
    assert not cfg.has_option("blast", "db-partition-prefix")


def test_auto_sharding_skipped_when_metadata_missing() -> None:
    # Route was unable to resolve the DB metadata — degrade silently
    # (don't crash the submit, just don't auto-shard).
    params = _base_params()
    params.pop("db_total_bytes")
    cfg = _parse(generate_config(params))
    assert not cfg.has_option("blast", "db-partitions")


def test_explicit_db_partitions_overrides_auto_sharding() -> None:
    # Power-user manually sets db_partitions → respect it, do NOT recompute.
    params = _base_params()
    params["db_partitions"] = 8
    cfg = _parse(generate_config(params))
    assert cfg.get("blast", "db-partitions") == "8"
    # The user-provided value should NOT trigger our auto-prefix injection.
    # If they want a custom prefix, they must set db_partition_prefix too.
    assert not cfg.has_option("blast", "db-partition-prefix")


def test_explicit_db_partitions_conflict_with_off_mode() -> None:
    params = _base_params()
    params["db_partitions"] = 8
    params["sharding_mode"] = "off"
    with pytest.raises(ValueError, match="conflicts"):
        generate_config(params)


def test_explicit_db_partition_prefix_overrides_auto_sharding() -> None:
    params = _base_params()
    params["db_partition_prefix"] = "https://example.com/custom_shard_"
    cfg = _parse(generate_config(params))
    assert cfg.get("blast", "db-partition-prefix") == "https://example.com/custom_shard_"
    # Did NOT auto-inject db-partitions either (caller is in full control).
    assert not cfg.has_option("blast", "db-partitions")


def test_auto_sharding_picks_higher_n_when_more_nodes() -> None:
    params = _base_params()
    params["allow_approximate_sharding"] = True
    params["num_nodes"] = 10
    cfg = _parse(generate_config(params))
    assert cfg.get("blast", "db-partitions") == "10"


def test_approximate_sharding_rejects_more_shards_than_nodes() -> None:
    params = _base_params()
    params["allow_approximate_sharding"] = True
    params["num_nodes"] = 1  # below the memory floor of 5 for 269 GB on E16
    with pytest.raises(ValueError, match="at least one node per shard"):
        generate_config(params)


@pytest.mark.parametrize(
    "machine_type,num_nodes,expected_n",
    [
        ("Standard_E32s_v5", 5, "5"),  # 256 GB RAM → mem floor 3 → nodes wins
        ("Standard_E64s_v5", 3, "3"),  # 512 GB RAM → mem floor 2 → nodes wins
    ],
)
def test_auto_sharding_respects_node_ram_capacity(
    machine_type: str, num_nodes: int, expected_n: str
) -> None:
    params = _base_params()
    params["allow_approximate_sharding"] = True
    params["machine_type"] = machine_type
    params["num_nodes"] = num_nodes
    cfg = _parse(generate_config(params))
    assert cfg.get("blast", "db-partitions") == expected_n
