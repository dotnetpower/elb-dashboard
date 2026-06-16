"""Tests for the wake-on-request AKS auto-start drain gate.

Responsibility: Exercise ``api.services.blast.cluster_autostart.evaluate_for_drain``
    across the readiness states it branches on (disabled / no-cluster / ready /
    stopped+pending / stopped+empty / starting / not_found / error) plus the
    debounce guard, asserting the load-bearing ``proceed_with_drain`` /
    ``started`` outcome and that ``start_aks`` is enqueued only when justified.
Edit boundaries: Test-only. Patches the readiness brain, the peek, the debounce,
    and the ``start_aks`` task so no real Azure / Redis / Celery is touched.
Key entry points: the ``test_*`` functions.
Risky contracts: A start side effect must fire ONLY for a stopped, start-
    recommended cluster with pending work and an open debounce window.
Validation: ``uv run pytest -q api/tests/test_cluster_autostart.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.blast import cluster_autostart
from api.services.service_bus_pref import ServiceBusConfig


def _cfg(**over: Any) -> ServiceBusConfig:
    base = dict(
        enabled=True,
        auth_mode="entra",
        namespace_fqdn="x.servicebus.windows.net",
        request_queue="elastic-blast-requests",
        completion_queue="elastic-blast-results",
        autostart_cluster_enabled=True,
        subscription_id="sub-1",
        resource_group="rg-1",
        cluster_name="aks-1",
    )
    base.update(over)
    return ServiceBusConfig(**base)


class _FakeAsyncResult:
    id = "task-xyz"


def _patch_start(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {"count": 0, "kwargs": None}

    class _FakeTask:
        def delay(self, **kwargs: Any) -> _FakeAsyncResult:
            calls["count"] += 1
            calls["kwargs"] = kwargs
            return _FakeAsyncResult()

    import api.tasks.azure as azure_tasks

    monkeypatch.setattr(azure_tasks, "start_aks", _FakeTask())
    return calls


def _patch_eval(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]) -> None:
    import api.services.aks.ensure_running as er

    monkeypatch.setattr(er, "evaluate_ensure_running", lambda *a, **k: result)
    monkeypatch.setattr(cluster_autostart, "_debounce_ok", lambda _name: True)
    monkeypatch.setattr(
        "api.services.get_credential", lambda *a, **k: object(), raising=False
    )


def test_disabled_proceeds_without_eval(monkeypatch: pytest.MonkeyPatch) -> None:
    decision = cluster_autostart.evaluate_for_drain(_cfg(autostart_cluster_enabled=False))
    assert decision.proceed_with_drain is True
    assert decision.started is False
    assert decision.status == "disabled"


def test_incomplete_cluster_context_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    decision = cluster_autostart.evaluate_for_drain(_cfg(cluster_name=""))
    assert decision.proceed_with_drain is True
    assert decision.status == "no_cluster"


def test_ready_cluster_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_eval(monkeypatch, {"status": "ready"})
    calls = _patch_start(monkeypatch)
    decision = cluster_autostart.evaluate_for_drain(_cfg())
    assert decision.proceed_with_drain is True
    assert decision.started is False
    assert calls["count"] == 0


def test_stopped_with_pending_starts_and_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_eval(monkeypatch, {"status": "stopped", "start_recommended": True})
    calls = _patch_start(monkeypatch)
    monkeypatch.setattr(cluster_autostart, "_has_pending_request", lambda _cfg: True)

    decision = cluster_autostart.evaluate_for_drain(_cfg())
    assert decision.proceed_with_drain is False
    assert decision.started is True
    assert decision.start_task_id == "task-xyz"
    assert calls["count"] == 1
    assert calls["kwargs"] == {
        "subscription_id": "sub-1",
        "resource_group": "rg-1",
        "cluster_name": "aks-1",
    }


def test_stopped_empty_queue_does_not_start(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_eval(monkeypatch, {"status": "stopped", "start_recommended": True})
    calls = _patch_start(monkeypatch)
    monkeypatch.setattr(cluster_autostart, "_has_pending_request", lambda _cfg: False)

    decision = cluster_autostart.evaluate_for_drain(_cfg())
    assert decision.proceed_with_drain is False
    assert decision.started is False
    assert decision.status == "no_pending"
    assert calls["count"] == 0


def test_stopped_not_recommended_holds_without_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_eval(monkeypatch, {"status": "stopped", "start_recommended": False})
    calls = _patch_start(monkeypatch)
    decision = cluster_autostart.evaluate_for_drain(_cfg())
    assert decision.proceed_with_drain is False
    assert decision.started is False
    assert calls["count"] == 0


def test_starting_holds_without_rekick(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_eval(monkeypatch, {"status": "starting"})
    calls = _patch_start(monkeypatch)
    decision = cluster_autostart.evaluate_for_drain(_cfg())
    assert decision.proceed_with_drain is False
    assert decision.status == "starting"
    assert calls["count"] == 0


def test_not_found_proceeds_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_eval(monkeypatch, {"status": "not_found"})
    decision = cluster_autostart.evaluate_for_drain(_cfg())
    assert decision.proceed_with_drain is True
    assert decision.status == "not_found"


def test_eval_error_degrades_to_proceed(monkeypatch: pytest.MonkeyPatch) -> None:
    import api.services.aks.ensure_running as er

    def _boom(*a: Any, **k: Any) -> dict[str, Any]:
        raise RuntimeError("arm down")

    monkeypatch.setattr(er, "evaluate_ensure_running", _boom)
    monkeypatch.setattr(
        "api.services.get_credential", lambda *a, **k: object(), raising=False
    )
    decision = cluster_autostart.evaluate_for_drain(_cfg())
    assert decision.proceed_with_drain is True
    assert decision.status == "error"


def test_debounce_blocks_second_kick(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_eval(monkeypatch, {"status": "stopped", "start_recommended": True})
    monkeypatch.setattr(cluster_autostart, "_has_pending_request", lambda _cfg: True)
    monkeypatch.setattr(cluster_autostart, "_debounce_ok", lambda _name: False)
    calls = _patch_start(monkeypatch)

    decision = cluster_autostart.evaluate_for_drain(_cfg())
    assert decision.proceed_with_drain is False
    assert decision.started is False
    assert calls["count"] == 0
