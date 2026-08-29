"""Regression tests for `k8s_release_stale_warmup_jobs`.

Responsibility: Regression tests for `k8s_release_stale_warmup_jobs`
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_make_job`, `_patch_session`,
`test_release_stale_warmup_jobs_deletes_only_jobs_on_dead_nodes`,
`test_release_stale_warmup_jobs_keeps_jobs_when_all_nodes_live`,
`test_release_stale_warmup_jobs_skips_jobs_without_node_pin`,
`test_release_stale_warmup_jobs_reports_partial_on_delete_error`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped. Forced cache release must not report success until
both matching Jobs and pods are absent.
Validation: `uv run pytest -q api/tests/test_k8s_release_stale_warmup_jobs.py`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from api.services.k8s import monitoring as km
from api.services.k8s import warmup_status as warmup_status_module


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


def test_release_warmup_cache_waits_for_jobs_and_pods_to_disappear() -> None:
    delete_response = MagicMock(status_code=200, text="")
    empty_response = MagicMock(status_code=200, text="")
    empty_response.json.return_value = {"items": []}
    session = MagicMock()
    session.delete.return_value = delete_response
    session.get.return_value = empty_response

    with patch.object(km, "_get_k8s_session", return_value=(session, "https://aks")), patch.object(
        km, "_namespace_or_default", return_value="default"
    ), patch.object(warmup_status_module.time, "sleep", return_value=None):
        out = km.k8s_release_warmup_cache(
            MagicMock(),
            "sub",
            "rg",
            "aks",
            "core_nt",
            wait_for_absence_seconds=30,
        )

    assert out["status"] == "released"
    assert out["absence_verified"] is True
    assert out["remaining_jobs"] == []
    assert out["remaining_pods"] == []
    assert out["remaining_legacy_daemonsets"] == []
    assert out["remaining_legacy_pods"] == []
    assert out["failure_reason"] == ""
    assert all(
        call.kwargs["params"]["propagationPolicy"] == "Foreground"
        for call in session.delete.call_args_list
    )
    selectors = {call.kwargs["params"]["labelSelector"] for call in session.get.call_args_list}
    assert selectors == {"app=elb-db-warmup,db=core_nt", "app=db-warmup,db=core_nt"}


def test_release_warmup_cache_timeout_reports_remaining_resources() -> None:
    delete_response = MagicMock(status_code=200, text="")
    remaining_job = {"metadata": {"name": "warm-core-nt-00"}}
    remaining_pod = {"metadata": {"name": "warm-core-nt-00-old"}}
    session = MagicMock()
    session.delete.return_value = delete_response
    clock = {"now": 0.0}

    def list_resources(url: str, *, params: dict[str, str], timeout: float):
        del timeout
        response = MagicMock(status_code=200, text="")
        selector = params["labelSelector"]
        if url.endswith("/jobs") and selector.startswith("app=elb-db-warmup"):
            items = [remaining_job]
        elif url.endswith("/pods") and selector.startswith("app=db-warmup"):
            items = [remaining_pod]
        else:
            items = []
        response.json.return_value = {"items": items}
        return response

    session.get.side_effect = list_resources

    with patch.object(km, "_get_k8s_session", return_value=(session, "https://aks")), patch.object(
        km, "_namespace_or_default", return_value="default"
    ), patch.object(
        warmup_status_module.time,
        "monotonic",
        side_effect=lambda: clock["now"],
    ), patch.object(
        warmup_status_module.time,
        "sleep",
        side_effect=lambda _seconds: clock.update(now=31.0),
    ):
        out = km.k8s_release_warmup_cache(
            MagicMock(),
            "sub",
            "rg",
            "aks",
            "core_nt",
            wait_for_absence_seconds=30,
        )

    assert out["status"] == "partial"
    assert out["absence_verified"] is False
    assert out["remaining_jobs"] == ["warm-core-nt-00"]
    assert out["remaining_pods"] == []
    assert out["remaining_legacy_daemonsets"] == []
    assert out["remaining_legacy_pods"] == ["warm-core-nt-00-old"]
    assert out["failure_reason"] == "deletion_timeout"
    assert out["errors"][-1]["reason"] == "deletion_timeout"
