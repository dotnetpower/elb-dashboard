"""Build query grouping plans for future mixed-search-space precise sharding.

This module is intentionally pure: it does not upload blobs, enqueue Celery
tasks, or call ElasticBLAST. It prepares the validated per-group FASTA payloads
and options that a future split dispatcher can execute.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from api.services.query_metadata import QueryMetadata
from api.services.sharding_precision import (
    query_effective_search_spaces,
    query_effective_search_spaces_error,
)

_SAFE_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class QueryGroupPlanItem:
    group_id: str
    effective_search_space: int
    query_indices: list[int]
    query_ids: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "effective_search_space": self.effective_search_space,
            "query_indices": self.query_indices,
            "query_ids": self.query_ids,
        }


@dataclass(frozen=True)
class QueryGroupPlan:
    query_count: int
    group_count: int
    requires_split: bool
    groups: list[QueryGroupPlanItem] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "query_count": self.query_count,
            "group_count": self.group_count,
            "requires_split": self.requires_split,
            "groups": [group.as_dict() for group in self.groups],
        }


@dataclass(frozen=True)
class QuerySplitExecutionItem:
    group_id: str
    child_job_id: str
    effective_search_space: int
    query_blob_path: str
    query_file: str
    query_fasta: str
    options: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "child_job_id": self.child_job_id,
            "effective_search_space": self.effective_search_space,
            "query_blob_path": self.query_blob_path,
            "query_file": self.query_file,
            "query_fasta_bytes": len(self.query_fasta.encode("utf-8")),
            "options": self.options,
        }


@dataclass(frozen=True)
class QuerySplitExecutionPlan:
    parent_job_id: str
    requires_split: bool
    groups: list[QuerySplitExecutionItem]

    def as_dict(self) -> dict[str, Any]:
        return {
            "parent_job_id": self.parent_job_id,
            "requires_split": self.requires_split,
            "groups": [group.as_dict() for group in self.groups],
        }


def build_query_group_plan(
    metadata: QueryMetadata,
    query_effective_search_spaces_value: object | None,
) -> QueryGroupPlan:
    """Group FASTA queries by effective search space while preserving query order."""
    validation_error = query_effective_search_spaces_error(query_effective_search_spaces_value)
    if validation_error is not None:
        raise ValueError(validation_error)
    spaces = query_effective_search_spaces(query_effective_search_spaces_value)
    if len(spaces) != metadata.query_count:
        raise ValueError("query_effective_search_spaces count must match query metadata")

    grouped: dict[int, QueryGroupPlanItem] = {}
    ordered_spaces: list[int] = []
    for index, (record, search_space) in enumerate(zip(metadata.records, spaces, strict=True)):
        if search_space not in grouped:
            ordered_spaces.append(search_space)
            grouped[search_space] = QueryGroupPlanItem(
                group_id=f"qg{len(ordered_spaces)}",
                effective_search_space=search_space,
                query_indices=[],
                query_ids=[],
            )
        group = grouped[search_space]
        group.query_indices.append(index)
        group.query_ids.append(record.query_id)

    groups = [grouped[search_space] for search_space in ordered_spaces]
    return QueryGroupPlan(
        query_count=metadata.query_count,
        group_count=len(groups),
        requires_split=len(groups) > 1,
        groups=groups,
    )


def materialize_group_fasta(metadata: QueryMetadata, group: QueryGroupPlanItem) -> str:
    """Render a FASTA document for one query group using original record order."""
    if not group.query_indices:
        raise ValueError("query group has no query indices")
    seen_indices: set[int] = set()
    records: list[str] = []
    for index in group.query_indices:
        if index in seen_indices:
            raise ValueError(f"duplicate query index in group: {index}")
        seen_indices.add(index)
        if index < 0 or index >= metadata.query_count:
            raise ValueError(f"query index out of range: {index}")
        records.append(metadata.records[index].as_fasta())
    return "".join(records)


def materialize_group_fastas(
    metadata: QueryMetadata,
    plan: QueryGroupPlan,
) -> dict[str, str]:
    """Render FASTA payloads for every group in a query grouping plan."""
    if plan.query_count != metadata.query_count:
        raise ValueError("query group plan does not match query metadata")
    return {group.group_id: materialize_group_fasta(metadata, group) for group in plan.groups}


def build_query_split_execution_plan(
    *,
    parent_job_id: str,
    metadata: QueryMetadata,
    query_effective_search_spaces_value: object | None,
    base_options: dict[str, Any] | None = None,
) -> QuerySplitExecutionPlan:
    """Create per-query-group execution inputs without performing side effects."""
    if not _SAFE_JOB_ID_RE.fullmatch(parent_job_id):
        raise ValueError("parent_job_id must be a safe blob path segment")

    plan = build_query_group_plan(metadata, query_effective_search_spaces_value)
    items: list[QuerySplitExecutionItem] = []
    for group in plan.groups:
        group_fasta = materialize_group_fasta(metadata, group)
        group_options = dict(base_options or {})
        group_options["sharding_mode"] = "precise"
        group_options["query_count"] = len(group.query_indices)
        group_options["db_effective_search_space"] = group.effective_search_space
        group_options["query_effective_search_spaces"] = [group.effective_search_space] * len(
            group.query_indices
        )

        query_blob_path = f"split/{parent_job_id}/{group.group_id}/query.fa"
        items.append(
            QuerySplitExecutionItem(
                group_id=group.group_id,
                child_job_id=f"{parent_job_id}-{group.group_id}",
                effective_search_space=group.effective_search_space,
                query_blob_path=query_blob_path,
                query_file=f"queries/{query_blob_path}",
                query_fasta=group_fasta,
                options=group_options,
            )
        )

    return QuerySplitExecutionPlan(
        parent_job_id=parent_job_id,
        requires_split=plan.requires_split,
        groups=items,
    )
