"""Unit tests for `k8s_pod_describe` formatter.

Tests `_format_pod_describe` as a pure function — no Kubernetes API calls
required. Covers the happy path (rich pod manifest + events), empty
labels/annotations, and missing/non-dict input tolerance.

Responsibility: Verify the kubectl-describe-like text emitted by
`_format_pod_describe`.
Edit boundaries: Keep assertions focused on the visible text contract.
Key entry points: `test_format_pod_describe_renders_core_fields`,
`test_format_pod_describe_tolerates_minimal_pod`,
`test_format_pod_describe_sorts_events_newest_first`.
Risky contracts: The output is consumed by the SPA "Describe" dialog as
plain text; field labels and the `Events:` header are load-bearing.
Validation: `uv run pytest -q api/tests/test_k8s_pod_describe.py`.
"""

from __future__ import annotations

from api.services.k8s.observability import _format_pod_describe


def _pod() -> dict[str, object]:
    return {
        "metadata": {
            "name": "warm-16s-ribosomal-rna-00-abc",
            "namespace": "default",
            "creationTimestamp": "2026-05-27T10:00:00Z",
            "labels": {"app": "warmup", "db": "16S_ribosomal_RNA"},
            "annotations": {"reason": "manual-restart"},
            "ownerReferences": [{"kind": "Job", "name": "warm-16s-ribosomal-rna-00"}],
        },
        "spec": {
            "nodeName": "aks-blastpool-12345-vmss000000",
            "serviceAccountName": "warmup-sa",
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "warmup",
                    "image": "myacr.azurecr.io/elasticblast-warmup:1.0.0",
                    "ports": [{"containerPort": 8080, "protocol": "TCP"}],
                    "resources": {
                        "requests": {"cpu": "500m", "memory": "1Gi"},
                        "limits": {"cpu": "2", "memory": "4Gi"},
                    },
                }
            ],
        },
        "status": {
            "phase": "Running",
            "podIP": "10.244.1.5",
            "hostIP": "10.224.0.4",
            "startTime": "2026-05-27T10:00:05Z",
            "conditions": [
                {"type": "Ready", "status": "True"},
                {"type": "Initialized", "status": "True"},
            ],
            "containerStatuses": [
                {
                    "name": "warmup",
                    "ready": True,
                    "restartCount": 0,
                    "imageID": "docker.io/library/warmup@sha256:deadbeef",
                    "state": {"running": {"startedAt": "2026-05-27T10:00:05Z"}},
                }
            ],
        },
    }


def test_format_pod_describe_renders_core_fields() -> None:
    text = _format_pod_describe(_pod(), [])

    # Top-level identity + status (labels are padded to width 18)
    assert "Name:             warm-16s-ribosomal-rna-00-abc" in text
    assert "Namespace:        default" in text
    assert "Node:             aks-blastpool-12345-vmss000000" in text
    assert "Status:           Running" in text
    assert "IP:               10.244.1.5" in text
    assert "Service Account:  warmup-sa" in text

    # Labels / annotations / owner
    assert "Labels:" in text
    assert "app=warmup" in text
    assert "Annotations:" in text
    assert "reason=manual-restart" in text
    assert "Controlled By:" in text
    assert "Job/warm-16s-ribosomal-rna-00" in text

    # Container detail
    assert "Containers:" in text
    assert "warmup:" in text
    assert "Image:          myacr.azurecr.io/elasticblast-warmup:1.0.0" in text
    assert "Ready:          True" in text
    assert "Restart Count:  0" in text
    assert "Ports:          8080/TCP" in text
    assert "Requests:" in text and "cpu=500m" in text
    assert "Limits:" in text and "memory=4Gi" in text
    assert "Running (started 2026-05-27T10:00:05Z)" in text

    # Conditions / events sections always present
    assert "Conditions:" in text
    assert "Ready" in text
    assert "Events:" in text
    assert "<none>" in text  # no events supplied


def test_format_pod_describe_tolerates_minimal_pod() -> None:
    text = _format_pod_describe({}, [])
    assert "Name:" in text
    assert "Labels:" in text and "  <none>" in text
    assert "Annotations:" in text
    assert "Containers:" in text and "  <none>" in text
    assert "Events:" in text


def test_format_pod_describe_sorts_events_newest_first() -> None:
    events = [
        {
            "type": "Warning",
            "reason": "BackOff",
            "message": "Back-off restarting failed container",
            "count": 3,
            "lastTimestamp": "2026-05-27T09:55:00Z",
        },
        {
            "type": "Normal",
            "reason": "Scheduled",
            "message": "Successfully assigned default/warmup to aks-...-000000",
            "count": 1,
            "lastTimestamp": "2026-05-27T10:00:00Z",
        },
    ]
    text = _format_pod_describe(_pod(), events)

    # Header line present once.
    assert text.count("Type") >= 1
    # Newest first → Scheduled should appear before BackOff in the output.
    scheduled_pos = text.index("Scheduled")
    backoff_pos = text.index("BackOff")
    assert scheduled_pos < backoff_pos

    # Both rows rendered with counts.
    assert "Scheduled" in text
    assert "BackOff" in text
    assert "3" in text


def test_format_pod_describe_handles_waiting_and_terminated_states() -> None:
    pod = _pod()
    status = pod["status"]
    assert isinstance(status, dict)
    container_statuses = status.get("containerStatuses")
    assert isinstance(container_statuses, list)
    container_statuses[0]["state"] = {
        "waiting": {"reason": "CrashLoopBackOff", "message": "Back-off 5m restarting"}
    }
    container_statuses[0]["lastState"] = {
        "terminated": {"exitCode": 137, "reason": "OOMKilled", "message": "killed"}
    }
    text = _format_pod_describe(pod, [])
    assert "Waiting (CrashLoopBackOff)" in text
    assert "Terminated (exit 137, OOMKilled)" in text
