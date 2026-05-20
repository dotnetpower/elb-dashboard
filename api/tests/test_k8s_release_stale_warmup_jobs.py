"""Regression tests for `k8s_release_stale_warmup_jobs`.

After AKS stop/start, succeeded warmup Jobs may still exist but be pinned to
VMSS node names that no longer belong to the cluster. Because
`spec.template.spec.nodeName` is immutable, the only way back to a Ready
state is to delete those Jobs so `k8s_ensure_job_manifests` can recreate
fresh ones on the current ready nodes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from api.services import k8s_monitoring as km


def _make_job(name: str, node: str, source_version: str = "") -> dict[str, Any]:
    annotations = {"elb.dashboard/source-version": source_version} if source_version else {}
    return {
        "metadata": {"name": name, "annotations": annotations},
        "spec": {
            "template": {
                "metadata": {"annotations": annotations},
                "spec": {"nodeName": node},
            }
        },
    }


def _patch_session(jobs: list[dict[str, Any]], delete_status: int = 200):
    list_response = MagicMock()
    list_response.status_code = 200
    list_response.json.return_value = {"items": jobs}

    delete_response = MagicMock()
    delete_response.status_code = delete_status
    delete_response.text = ""

    session = MagicMock()
    session.get.return_value = list_response
    session.delete.return_value = delete_response
    session.close = MagicMock()
    return (
        session,
        patch.object(km, "_get_k8s_session", return_value=(session, "https://aks")),
        patch.object(km, "_namespace_or_default", return_value="default"),
    )


def test_release_stale_warmup_jobs_deletes_only_jobs_on_dead_nodes() -> None:
    jobs = [
        _make_job("warm-core-nt-00", "aks-blastpool-OLD-vmss00000a"),
        _make_job("warm-core-nt-01", "aks-blastpool-NEW-vmss00000b"),
        _make_job("warm-core-nt-02", "aks-blastpool-OLD-vmss00000c"),
    ]
    session, session_patch, ns_patch = _patch_session(jobs)
    with session_patch, ns_patch:
        out = km.k8s_release_stale_warmup_jobs(
            MagicMock(),
            "sub",
            "rg",
            "aks",
            "core_nt",
            current_node_names=["aks-blastpool-NEW-vmss00000b"],
        )
    assert out["status"] == "released"
    deleted_names = sorted(item["name"] for item in out["deleted"])
    assert deleted_names == ["warm-core-nt-00", "warm-core-nt-02"]
    assert out["kept"] == ["warm-core-nt-01"]
    # Two delete calls, both with background propagation.
    assert session.delete.call_count == 2
    for call in session.delete.call_args_list:
        assert call.kwargs["params"]["propagationPolicy"] == "Background"


def test_release_stale_warmup_jobs_keeps_jobs_when_all_nodes_live() -> None:
    jobs = [
        _make_job("warm-core-nt-00", "node-a"),
        _make_job("warm-core-nt-01", "node-b"),
    ]
    session, session_patch, ns_patch = _patch_session(jobs)
    with session_patch, ns_patch:
        out = km.k8s_release_stale_warmup_jobs(
            MagicMock(),
            "sub",
            "rg",
            "aks",
            "core_nt",
            current_node_names=["node-a", "node-b"],
        )
    assert out["status"] == "released"
    assert out["deleted"] == []
    assert sorted(out["kept"]) == ["warm-core-nt-00", "warm-core-nt-01"]
    assert session.delete.call_count == 0


def test_release_stale_warmup_jobs_skips_jobs_without_node_pin() -> None:
    # A Job without an explicit nodeName cannot be classified as stale
    # by node identity, so leave it alone.
    jobs = [
        {"metadata": {"name": "warm-core-nt-foo"}, "spec": {"template": {"spec": {}}}},
    ]
    session, session_patch, ns_patch = _patch_session(jobs)
    with session_patch, ns_patch:
        out = km.k8s_release_stale_warmup_jobs(
            MagicMock(),
            "sub",
            "rg",
            "aks",
            "core_nt",
            current_node_names=[],
        )
    assert out["status"] == "released"
    assert out["deleted"] == []
    assert out["kept"] == ["warm-core-nt-foo"]
    assert session.delete.call_count == 0


def test_release_stale_warmup_jobs_reports_partial_on_delete_error() -> None:
    jobs = [_make_job("warm-core-nt-00", "dead-node")]
    _session, session_patch, ns_patch = _patch_session(jobs, delete_status=500)
    with session_patch, ns_patch:
        out = km.k8s_release_stale_warmup_jobs(
            MagicMock(),
            "sub",
            "rg",
            "aks",
            "core_nt",
            current_node_names=["live-node"],
        )
    assert out["status"] == "partial"
    assert out["deleted"] == []
    assert out["errors"][0]["name"] == "warm-core-nt-00"
    assert out["errors"][0]["status_code"] == 500


def test_release_stale_warmup_jobs_deletes_jobs_from_old_source_version() -> None:
    jobs = [
        _make_job("warm-core-nt-00", "node-a", source_version="old"),
        _make_job("warm-core-nt-01", "node-b", source_version="new"),
        _make_job("warm-core-nt-02", "node-c"),
    ]
    _session, session_patch, ns_patch = _patch_session(jobs)
    with session_patch, ns_patch:
        out = km.k8s_release_stale_warmup_jobs(
            MagicMock(),
            "sub",
            "rg",
            "aks",
            "core_nt",
            current_node_names=["node-a", "node-b", "node-c"],
            current_source_version="new",
        )

    assert out["status"] == "released"
    assert [item["name"] for item in out["deleted"]] == [
        "warm-core-nt-00",
        "warm-core-nt-02",
    ]
    assert out["deleted"][0]["stale_source_version"] == "old"
    assert out["deleted"][1]["stale_source_version"] == ""
    assert out["kept"] == ["warm-core-nt-01"]
