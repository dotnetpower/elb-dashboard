"""Tests for bounded Auto oracle reconciliation.

Responsibility: Verify default-off behavior, owner-RBAC fail-closed gating,
    retry/cooldown skips, ready no-ops, and the per-tick enqueue cap.
Edit boundaries: Service/task dependencies are mocked; no cloud mutation.
Key entry points: `test_reconcile_defaults_disabled`,
    `test_reconcile_skips_unauthorized_and_backoff`,
    `test_reconcile_caps_accepted_dispatches`.
Risky contracts: Expected blockers must not enqueue and no tick may exceed the
    configured build cap.
Validation: `uv run pytest -q api/tests/test_auto_oracle_reconcile.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from api.services.auto_oracle import AutoOraclePreference
from api.services.auto_oracle_reconcile import reconcile_auto_oracle_preferences
from api.tasks.storage.reconcile_auto_oracle import reconcile_auto_oracle


@pytest.fixture(autouse=True)
def _auto_warm_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.auto_oracle_dependency_ready",
        lambda *_args: (True, "ready"),
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.get_auto_oracle_scan_cursor",
        lambda _name: "",
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.save_auto_oracle_scan_cursor",
        lambda _name, _cursor: None,
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.get_auto_oracle_preference",
        lambda subscription_id, cluster_resource_group, cluster_name, storage_account, db_name: (
            _pref(db_name, storage_account)
        ),
    )


def _pref(db_name: str, storage_account: str = "stelbtest") -> AutoOraclePreference:
    return AutoOraclePreference(
        subscription_id="sub-1",
        cluster_resource_group="rg-aks",
        cluster_name="aks-1",
        storage_resource_group="rg-storage",
        storage_account=storage_account,
        db_name=db_name,
        acr_name="acrelb",
        enabled=True,
        owner_oid="owner-1",
    )


def test_reconcile_defaults_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTO_ORACLE_RECONCILE_ENABLED", raising=False)

    result = reconcile_auto_oracle_preferences(
        credential=object(),
        send_task=lambda *_args, **_kwargs: None,
    )

    assert result["status"] == "disabled"
    assert result["inspected"] == 0


def test_reconcile_refuses_execution_without_rbac_enforcement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTO_ORACLE_RECONCILE_ENABLED", "true")
    monkeypatch.delenv("ENFORCE_AUTO_ORACLE_RBAC", raising=False)

    result = reconcile_auto_oracle_preferences(
        credential=object(),
        send_task=lambda *_args, **_kwargs: pytest.fail("must remain dormant"),
    )

    assert result["status"] == "disabled"


def test_auto_oracle_rbac_guard_defaults_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services.auto_oracle_reconcile import (
        auto_oracle_owner_authorized,
    )

    monkeypatch.delenv("ENFORCE_AUTO_ORACLE_RBAC", raising=False)
    monkeypatch.setenv("CONTAINER_APP_NAME", "ca-elb-dashboard")
    monkeypatch.setattr(
        "api.services.me_permissions.compute_caller_permissions",
        lambda *_args, **_kwargs: pytest.fail("legacy guard-off must not enumerate"),
    )

    assert auto_oracle_owner_authorized(object(), _pref("core_nt")) == (
        True,
        "legacy_guard_off",
    )


def test_auto_oracle_rbac_guard_on_fails_closed_on_degraded_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services.auto_oracle_reconcile import (
        auto_oracle_owner_authorized,
    )

    monkeypatch.setenv("ENFORCE_AUTO_ORACLE_RBAC", "true")
    monkeypatch.setenv("CONTAINER_APP_NAME", "ca-elb-dashboard")
    monkeypatch.setattr(
        "api.services.me_permissions.compute_caller_permissions",
        lambda *_args, **_kwargs: SimpleNamespace(
            degraded=True,
            can_write=True,
        ),
    )

    assert auto_oracle_owner_authorized(object(), _pref("core_nt")) == (
        False,
        "cluster_permission_indeterminate",
    )


def test_reconcile_skips_unauthorized_and_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefs = [_pref("core_nt"), _pref("nt")]
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.list_auto_oracle_preference_page",
        lambda **_kwargs: (prefs, "next-page"),
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.oracle_container",
        lambda *_args: object(),
    )
    decisions = iter([(False, "storage_write_denied"), (True, "authorized")])
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.auto_oracle_owner_authorized",
        lambda *_args: next(decisions),
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.read_oracle_automation",
        lambda _container, db_name: (
            {} if db_name == "core_nt" else {"next_retry_at": "2999-01-01T00:00:00+00:00"}
        ),
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile._mark_blocked",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.start_oracle_build",
        lambda *_args, **_kwargs: pytest.fail("dispatch must not run"),
    )

    result = reconcile_auto_oracle_preferences(
        credential=object(),
        send_task=lambda *_args, **_kwargs: None,
        enabled=True,
    )

    assert {item["reason"] for item in result["skipped"]} == {
        "storage_write_denied",
        "retry_backoff",
    }
    assert result["enqueued"] == []


def test_reconcile_rechecks_auto_warm_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.list_auto_oracle_preference_page",
        lambda **_kwargs: ([_pref("core_nt")], ""),
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.oracle_container",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.auto_oracle_dependency_ready",
        lambda *_args: (False, "auto_warm_disabled"),
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.read_oracle_automation",
        lambda *_args: {},
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile._mark_blocked",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.start_oracle_build",
        lambda *_args, **_kwargs: pytest.fail("dispatch must not run"),
    )

    result = reconcile_auto_oracle_preferences(
        credential=object(),
        send_task=lambda *_args, **_kwargs: None,
        enabled=True,
    )

    assert result["skipped"] == [{"db": "core_nt", "reason": "auto_warm_disabled"}]


def test_reconcile_skips_snapshot_disabled_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.list_auto_oracle_preference_page",
        lambda **_kwargs: ([_pref("core_nt")], ""),
    )
    disabled = AutoOraclePreference.from_dict({**_pref("core_nt").to_dict(), "enabled": False})
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.get_auto_oracle_preference",
        lambda *_args: disabled,
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.start_oracle_build",
        lambda *_args, **_kwargs: pytest.fail("disabled preference must not dispatch"),
    )

    result = reconcile_auto_oracle_preferences(
        credential=object(),
        send_task=lambda *_args, **_kwargs: None,
        enabled=True,
    )

    assert result["skipped"] == [{"db": "core_nt", "reason": "preference_disabled"}]


def test_blocked_state_and_event_are_deduplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services.auto_oracle_reconcile import _mark_blocked

    writes = []
    events = []
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.update_oracle_automation",
        lambda *_args, **kwargs: writes.append(kwargs) or kwargs,
    )
    monkeypatch.setattr(
        "api.services.feature_events.record_feature_event",
        lambda event, **kwargs: events.append((event, kwargs)),
    )

    _mark_blocked(object(), db_name="core_nt", reason="auto_warm_disabled")
    _mark_blocked(
        object(),
        db_name="core_nt",
        reason="auto_warm_disabled",
        current_state={
            "status": "blocked",
            "blocked_reason": "auto_warm_disabled",
        },
    )

    assert len(writes) == 1
    assert events == [
        (
            "oracle_automation_blocked",
            {
                "status": "info",
                "database": "core_nt",
                "reason": "auto_warm_disabled",
            },
        )
    ]


def test_reconcile_caps_accepted_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefs = [_pref(f"db{index}", f"stelb{index}") for index in range(5)]
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.list_auto_oracle_preference_page",
        lambda **_kwargs: (prefs, ""),
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.oracle_container",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.auto_oracle_owner_authorized",
        lambda *_args: (True, "authorized"),
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.read_oracle_automation",
        lambda *_args: {},
    )
    calls = []

    def _dispatch(*_args, **kwargs):
        calls.append(kwargs["db_name"])
        return SimpleNamespace(
            accepted=True,
            run_id=f"run-{kwargs['db_name']}",
            task_id=f"task-{kwargs['db_name']}",
            status="queued",
        )

    monkeypatch.setattr("api.services.auto_oracle_reconcile.start_oracle_build", _dispatch)

    result = reconcile_auto_oracle_preferences(
        credential=object(),
        send_task=lambda *_args, **_kwargs: None,
        enabled=True,
    )

    assert len(calls) == 2
    assert len(result["enqueued"]) == 2


def test_reconcile_caps_each_storage_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefs = [_pref("core_nt"), _pref("nt"), _pref("refseq_rna")]
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.list_auto_oracle_preference_page",
        lambda **_kwargs: (prefs, ""),
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.oracle_container",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.auto_oracle_owner_authorized",
        lambda *_args: (True, "authorized"),
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.read_oracle_automation",
        lambda *_args: {},
    )
    calls = []
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.start_oracle_build",
        lambda *_args, **kwargs: (
            calls.append(kwargs["db_name"])
            or SimpleNamespace(
                accepted=True,
                run_id="run-1",
                task_id="task-1",
                status="queued",
            )
        ),
    )

    result = reconcile_auto_oracle_preferences(
        credential=object(),
        send_task=lambda *_args, **_kwargs: None,
        enabled=True,
    )

    assert len(calls) == 1
    assert [item["reason"] for item in result["skipped"]] == [
        "storage_enqueue_cap",
        "storage_enqueue_cap",
    ]


def test_reconcile_advances_durable_preference_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_calls = []
    cursor_writes = []
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.get_auto_oracle_scan_cursor",
        lambda name: "page-10" if name == "reconcile" else "",
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.list_auto_oracle_preference_page",
        lambda **kwargs: page_calls.append(kwargs) or ([], "page-11"),
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.save_auto_oracle_scan_cursor",
        lambda name, cursor: cursor_writes.append((name, cursor)),
    )

    result = reconcile_auto_oracle_preferences(
        credential=object(),
        send_task=lambda *_args, **_kwargs: None,
        enabled=True,
    )

    assert result["status"] == "completed"
    assert page_calls == [
        {
            "limit": 50,
            "continuation_token": "page-10",
            "enabled_only": True,
        }
    ]
    assert cursor_writes == [("reconcile", "page-11")]


def test_reconcile_cursor_failure_is_visible_and_replays_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.list_auto_oracle_preference_page",
        lambda **_kwargs: ([], "page-11"),
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.save_auto_oracle_scan_cursor",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("table unavailable")),
    )

    result = reconcile_auto_oracle_preferences(
        credential=object(),
        send_task=lambda *_args, **_kwargs: None,
        enabled=True,
    )

    assert result["status"] == "partial"
    assert result["errors"] == [{"error": "reconcile_cursor_write_failed"}]


def test_reconcile_resets_invalid_cursor_and_processes_first_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.get_auto_oracle_scan_cursor",
        lambda _name: "corrupt-token",
    )
    calls = []

    def _page(**kwargs):
        calls.append(kwargs["continuation_token"])
        if kwargs["continuation_token"]:
            raise ValueError("invalid token")
        return [], "fresh-next"

    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.list_auto_oracle_preference_page",
        _page,
    )
    writes = []
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.save_auto_oracle_scan_cursor",
        lambda name, cursor: writes.append((name, cursor)),
    )

    result = reconcile_auto_oracle_preferences(
        credential=object(),
        send_task=lambda *_args, **_kwargs: None,
        enabled=True,
    )

    assert calls == ["corrupt-token", ""]
    assert writes == [("reconcile", "fresh-next")]
    assert result["cursor_reset"] is True


def test_reconcile_task_fails_closed_when_lock_backend_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "api.services.redis_clients.get_ops_redis_client",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("redis down")),
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.reconcile_auto_oracle_preferences",
        lambda **_kwargs: pytest.fail("reconcile must not run without its lock"),
    )

    result = reconcile_auto_oracle.run()

    assert result == {"status": "skipped", "reason": "reconcile_lock_unavailable"}


def test_reconcile_task_releases_only_its_lock_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class _Redis:
        def set(self, key, token, *, nx, ex):
            calls["set"] = (key, token, nx, ex)
            return True

        def eval(self, script, count, key, token):
            calls["eval"] = (script, count, key, token)
            return 1

    monkeypatch.setattr(
        "api.services.redis_clients.get_ops_redis_client",
        lambda **_kwargs: _Redis(),
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.reconcile_auto_oracle_preferences",
        lambda **_kwargs: {"status": "completed"},
    )

    result = reconcile_auto_oracle.run()

    assert result == {"status": "completed"}
    set_key, set_token, _nx, _ex = calls["set"]
    _script, _count, eval_key, eval_token = calls["eval"]
    assert set_key == eval_key == "autooracle:reconcile:lock"
    assert set_token == eval_token
