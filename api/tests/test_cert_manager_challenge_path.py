"""Tests for the cert-manager Challenge / Order failure paths.

Module docstring (natural):
Pin the regression scenarios that the live 2026-05-28 incident exposed
but the previous unit suite did not catch:

* The HTTP-01 solver Pod stays `Pending` because the only schedulable
  pool is the blastpool and the solver Pod carries no toleration. The
  fix lives in `build_cluster_issuer` (solver `podTemplate`); here we
  pin that the constructed ClusterIssuer payload would let the solver
  Pod schedule.
* The `wait_certificate_ready` helper times out and the pipeline-level
  `except` injects a `diagnostics` string into the failed task result.

Responsibility: Unit tests for the cert-manager surface — pure
    construction + the wait/collector exception path. Real ACME issuance
    is exercised by the live deploy smoke.
Edit boundaries: Behavioural pins only; no Azure / kubectl I/O.
Key entry points: `test_solver_podtemplate_can_schedule_on_blastpool`,
    `test_wait_certificate_ready_attaches_diagnostics_on_timeout`.
Risky contracts: Mirrors the production helper names (`build_cluster_issuer`,
    `_wait_for_certificate_ready`, `_collect_cert_issuance_diagnostics`).
Validation: `uv run pytest -q api/tests/test_cert_manager_challenge_path.py`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from api.services.k8s.ingress import (
    WORKLOAD_POOL_NODE_SELECTOR,
    WORKLOAD_POOL_TOLERATION,
    build_cluster_issuer,
)
from api.tasks.openapi import public_https as task_module


def _node_tolerates_blastpool(spec: dict[str, Any]) -> bool:
    selector_match = (
        spec.get("nodeSelector", {}).get("kubernetes.azure.com/mode")
        == WORKLOAD_POOL_NODE_SELECTOR["kubernetes.azure.com/mode"]
    )
    tolerates_blast = any(
        t.get("key") == WORKLOAD_POOL_TOLERATION["key"]
        and t.get("value") == WORKLOAD_POOL_TOLERATION["value"]
        for t in spec.get("tolerations", [])
    )
    return selector_match and tolerates_blast


def test_solver_podtemplate_can_schedule_on_blastpool() -> None:
    """ClusterIssuer's solver Pod must carry blastpool-friendly spec.

    Regression guard for the 2026-05-28 incident where the
    `cm-acme-http-solver-*` Pod sat in `Pending` and Let's Encrypt
    timed out the challenge with a 503.
    """
    issuer = json.loads(build_cluster_issuer(email="ops@example.com"))
    pod_spec = (
        issuer["spec"]["acme"]["solvers"][0]["http01"]["ingress"]["podTemplate"]["spec"]
    )
    assert _node_tolerates_blastpool(pod_spec), (
        "solver podTemplate must schedule on the blastpool — otherwise the "
        "Challenge Pod cannot run and Let's Encrypt rejects the HTTP-01 "
        "challenge with 503"
    )


def test_wait_certificate_ready_attaches_diagnostics_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the wait helper times out, the diagnostic collector must run.

    The pipeline-level `except` catches the resulting RuntimeError and
    surfaces the diagnostics in the failed task result so the SPA shows
    the real cert-manager Challenge reason without an operator needing
    to `kubectl describe`.
    """
    seen: dict[str, Any] = {}

    def fake_kubectl_run(args: list[str], **_kw: Any) -> dict[str, Any]:
        # Simulate the kubectl `wait` returning non-zero (timeout).
        if args and args[0] == "wait":
            return {
                "exit_code": 1,
                "stdout": "",
                "stderr": "error: timed out waiting for the condition",
            }
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    def fake_existence_probe(*, kubeconfig_path: str) -> None:
        seen["existence"] = kubeconfig_path

    def fake_diagnostics(*, kubeconfig_path: str) -> str:
        seen["diagnostics"] = kubeconfig_path
        return (
            "certificate.condition: Ready=False(IssuanceFailed)\n"
            "challenge.reason: pending: wrong status code '503'"
        )

    monkeypatch.setattr(task_module, "kubectl_run", fake_kubectl_run)
    monkeypatch.setattr(
        task_module,
        "_wait_for_certificate_object_to_exist",
        fake_existence_probe,
    )
    monkeypatch.setattr(
        task_module,
        "_collect_cert_issuance_diagnostics",
        fake_diagnostics,
    )

    with pytest.raises(RuntimeError) as exc_info:
        task_module._wait_for_certificate_ready(
            kubeconfig_path="/tmp/fake-kc",  # noqa: S108 - test stub
            timeout_seconds=1,
        )
    msg = str(exc_info.value)
    assert "did not become Ready" in msg
    assert "challenge.reason: pending" in msg
    # The diagnostics collector must have been called with the same kubeconfig.
    assert seen.get("diagnostics") == "/tmp/fake-kc"  # noqa: S108 - test stub
    assert seen.get("existence") == "/tmp/fake-kc"  # noqa: S108 - test stub
