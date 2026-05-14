"""Tests for monitoring route boundary hardening helpers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from routes import monitor


def _error(response) -> str:
    return json.loads(response.get_body().decode("utf-8"))["error"]


def test_validate_monitor_scope_accepts_valid_cluster_scope() -> None:
    response = monitor._validate_monitor_scope(
        {
            "subscription_id": "00000000-0000-0000-0000-000000000000",
            "resource_group": "rg-elb",
            "cluster_name": "elastic-blast-01",
        },
        cluster_field="cluster_name",
    )

    assert response is None


def test_validate_monitor_scope_rejects_bad_subscription() -> None:
    response = monitor._validate_monitor_scope(
        {
            "subscription_id": "not-a-subscription",
            "resource_group": "rg-elb",
        }
    )

    assert response is not None
    assert response.status_code == 400
    assert "subscription_id" in _error(response)


def test_validate_k8s_name_rejects_path_like_value() -> None:
    response = monitor._validate_k8s_name("../default", "namespace")

    assert response is not None
    assert response.status_code == 400
    assert "namespace" in _error(response)


def test_validate_k8s_name_rejects_shell_like_value() -> None:
    response = monitor._validate_k8s_name("pod;delete", "pod_name")

    assert response is not None
    assert response.status_code == 400
    assert "pod_name" in _error(response)
