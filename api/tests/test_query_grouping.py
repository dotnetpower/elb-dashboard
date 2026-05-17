from __future__ import annotations

import pytest
from api.services.query_grouping import (
    QueryGroupPlan,
    QueryGroupPlanItem,
    build_query_group_plan,
    build_query_split_execution_plan,
    materialize_group_fasta,
    materialize_group_fastas,
)
from api.services.query_metadata import parse_fasta_metadata


def test_uniform_query_group_plan_does_not_require_split() -> None:
    metadata = parse_fasta_metadata(">q1\nAAAA\n>q2\nCCCC\n")

    plan = build_query_group_plan(metadata, [225, 225])

    assert plan.query_count == 2
    assert plan.group_count == 1
    assert plan.requires_split is False
    assert plan.groups[0].effective_search_space == 225
    assert plan.groups[0].query_indices == [0, 1]
    assert plan.groups[0].query_ids == ["q1", "q2"]


def test_mixed_query_group_plan_groups_by_search_space_in_first_seen_order() -> None:
    metadata = parse_fasta_metadata(">q1\nAAAA\n>q2\nCCCC\n>q3\nGGGG\n")

    plan = build_query_group_plan(metadata, [225, 300, 225])

    assert plan.group_count == 2
    assert plan.requires_split is True
    assert [group.group_id for group in plan.groups] == ["qg1", "qg2"]
    assert [group.effective_search_space for group in plan.groups] == [225, 300]
    assert plan.groups[0].query_indices == [0, 2]
    assert plan.groups[0].query_ids == ["q1", "q3"]
    assert plan.groups[1].query_indices == [1]
    assert plan.groups[1].query_ids == ["q2"]


def test_query_group_plan_rejects_count_mismatch() -> None:
    metadata = parse_fasta_metadata(">q1\nAAAA\n>q2\nCCCC\n")

    with pytest.raises(ValueError, match="count"):
        build_query_group_plan(metadata, [225])


def test_query_group_plan_rejects_mapping_input() -> None:
    metadata = parse_fasta_metadata(">q1\nAAAA\n>q2\nCCCC\n")

    with pytest.raises(ValueError, match="list ordered"):
        build_query_group_plan(metadata, {"q1": 225, "q2": 225})


def test_materialize_group_fastas_preserves_headers_sequences_and_group_order() -> None:
    metadata = parse_fasta_metadata(
        ">q1 first description\nAAAA\nAA\n"
        ">q2 second description\nCCCC\n"
        ">q3 third description\nGG GG\n"
    )
    plan = build_query_group_plan(metadata, [225, 300, 225])

    fastas = materialize_group_fastas(metadata, plan)

    assert list(fastas) == ["qg1", "qg2"]
    assert fastas["qg1"] == ">q1 first description\nAAAA\nAA\n>q3 third description\nGG GG\n"
    assert fastas["qg2"] == ">q2 second description\nCCCC\n"
    assert parse_fasta_metadata(fastas["qg1"]).records[1].query_id == "q3"


def test_materialize_group_fasta_rejects_bad_indices() -> None:
    metadata = parse_fasta_metadata(">q1\nAAAA\n")
    group = QueryGroupPlanItem(
        group_id="qg1",
        effective_search_space=225,
        query_indices=[1],
        query_ids=["q2"],
    )

    with pytest.raises(ValueError, match="out of range"):
        materialize_group_fasta(metadata, group)


def test_materialize_group_fastas_rejects_plan_metadata_mismatch() -> None:
    metadata = parse_fasta_metadata(">q1\nAAAA\n")
    plan = QueryGroupPlan(query_count=2, group_count=0, requires_split=False, groups=[])

    with pytest.raises(ValueError, match="does not match"):
        materialize_group_fastas(metadata, plan)


def test_build_query_split_execution_plan_sets_group_specific_inputs() -> None:
    metadata = parse_fasta_metadata(">q1 first\nAAAA\n>q2 second\nCCCC\n>q3 third\nGGGG\n")

    plan = build_query_split_execution_plan(
        parent_job_id="job-123",
        metadata=metadata,
        query_effective_search_spaces_value=[225, 300, 225],
        base_options={"outfmt": 6, "max_target_seqs": 10, "db_effective_search_space": 999},
    )

    assert plan.parent_job_id == "job-123"
    assert plan.requires_split is True
    assert [item.group_id for item in plan.groups] == ["qg1", "qg2"]
    assert [item.child_job_id for item in plan.groups] == ["job-123-qg1", "job-123-qg2"]
    assert plan.groups[0].query_blob_path == "split/job-123/qg1/query.fa"
    assert plan.groups[0].query_file == "queries/split/job-123/qg1/query.fa"
    assert plan.groups[0].query_fasta == ">q1 first\nAAAA\n>q3 third\nGGGG\n"
    assert plan.groups[0].options["db_effective_search_space"] == 225
    assert plan.groups[0].options["query_effective_search_spaces"] == [225, 225]
    assert plan.groups[0].options["query_count"] == 2
    assert plan.groups[1].query_fasta == ">q2 second\nCCCC\n"
    assert plan.groups[1].options["db_effective_search_space"] == 300
    assert plan.groups[1].options["query_effective_search_spaces"] == [300]
    assert plan.groups[1].options["max_target_seqs"] == 10


def test_query_split_execution_plan_as_dict_omits_raw_fasta() -> None:
    metadata = parse_fasta_metadata(">q1\nAAAA\n")

    plan = build_query_split_execution_plan(
        parent_job_id="job-123",
        metadata=metadata,
        query_effective_search_spaces_value=[225],
    )

    payload = plan.as_dict()
    assert "query_fasta" not in payload["groups"][0]
    assert payload["groups"][0]["query_fasta_bytes"] == len(b">q1\nAAAA\n")


def test_query_split_execution_plan_rejects_unsafe_parent_job_id() -> None:
    metadata = parse_fasta_metadata(">q1\nAAAA\n")

    for parent_job_id in ("../job-123", "job 123", "job?123", ""):
        with pytest.raises(ValueError, match="safe blob"):
            build_query_split_execution_plan(
                parent_job_id=parent_job_id,
                metadata=metadata,
                query_effective_search_spaces_value=[225],
            )
