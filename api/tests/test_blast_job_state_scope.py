"""Unit tests for the post-OData scope rechecker in api.services.blast.job_state.

Responsibility: Lock in the cluster-precedence semantics of
``_local_state_matches_job_scope`` so a row whose ``resource_group``
records the cluster RG (e.g. ``rg-elb-cluster``) still passes the
recheck when the caller is filtering with a different workspace RG
(e.g. ``rg-elb-dashboard``) but the same ``cluster_name``.
Edit boundaries: Pure-function tests; no Azure or HTTP I/O. If the
semantics ever need to invert (RG-as-hard-filter), update the
storage-layer test in ``test_state_repo.py`` in the same change.
Key entry points: ``test_*`` functions below.
Risky contracts: The rechecker is paired with
``JobStateRepository.list_for_scope``; both must agree.
Validation: ``uv run pytest -q api/tests/test_blast_job_state_scope.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.services.blast.job_state import _local_state_matches_job_scope


def _row(**overrides: object) -> SimpleNamespace:
    base = dict(
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
        cluster_name="elb-cluster-01",
        payload={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_matches_when_cluster_name_matches_and_rg_differs() -> None:
    """Production bug repro: cluster RG vs. workspace RG must not hide the row."""
    row = _row()  # row was saved with cluster RG
    assert _local_state_matches_job_scope(
        row,
        subscription_id="sub-1",
        resource_group="rg-elb-dashboard",  # caller's workspace RG
        cluster_name="elb-cluster-01",
    )


def test_rejects_when_cluster_name_mismatches() -> None:
    row = _row()
    assert not _local_state_matches_job_scope(
        row,
        subscription_id="sub-1",
        resource_group="",
        cluster_name="elb-cluster-02",
    )


def test_rejects_when_subscription_mismatches_even_with_matching_cluster() -> None:
    row = _row()
    assert not _local_state_matches_job_scope(
        row,
        subscription_id="sub-other",
        resource_group="",
        cluster_name="elb-cluster-01",
    )


def test_rg_acts_as_hard_filter_when_cluster_name_omitted() -> None:
    row = _row()
    assert not _local_state_matches_job_scope(
        row,
        subscription_id="sub-1",
        resource_group="rg-elb-dashboard",
        cluster_name="",
    )
    assert _local_state_matches_job_scope(
        row,
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
        cluster_name="",
    )


def test_falls_back_to_payload_values_when_top_level_columns_empty() -> None:
    row = SimpleNamespace(
        subscription_id=None,
        resource_group=None,
        cluster_name=None,
        payload={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb-cluster",
            "aks_cluster_name": "elb-cluster-01",
        },
    )
    assert _local_state_matches_job_scope(
        row,
        subscription_id="sub-1",
        resource_group="rg-elb-dashboard",
        cluster_name="elb-cluster-01",
    )


def test_empty_filters_pass_through() -> None:
    """All filters empty = no constraint."""
    row = _row()
    assert _local_state_matches_job_scope(
        row,
        subscription_id="",
        resource_group="",
        cluster_name="",
    )
