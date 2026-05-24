"""Tests for the method-aware HTTP inspector exclusion.

Responsibility: Confirm that high-volume polling GETs are excluded from
the per-request DETAIL inspector while POSTs / DELETEs on the same paths
remain captured.
Edit boundaries: Keep assertions focused on `_inspector_should_capture` —
no full FastAPI app boot needed.
Key entry points: `test_get_polling_paths_excluded`,
`test_non_get_methods_still_captured_on_polling_paths`,
`test_sse_and_health_paths_excluded_for_all_methods`
Risky contracts: Keep backward compatibility for single-arg call sites.
Validation: `uv run pytest -q api/tests/test_inspector_exclude.py`.
"""

from __future__ import annotations

import pytest
from api.app.inspector import _inspector_should_record
from api.main import _inspector_should_capture


@pytest.mark.parametrize(
    "path",
    [
        "/api/monitor/aks",
        "/api/monitor/aks/pods",
        "/api/monitor/storage",
        "/api/monitor/acr",
        "/api/monitor/cluster",
        "/api/monitor/jobs",
        "/api/blast/jobs",
        "/api/blast/jobs/abc-123",
        "/api/blast/databases",
        "/api/warmup/status",
        "/api/me",
    ],
)
def test_get_polling_paths_excluded(path: str) -> None:
    assert _inspector_should_capture(path, "GET") is False
    assert _inspector_should_record(path) is True


@pytest.mark.parametrize(
    "method",
    ["POST", "PUT", "PATCH", "DELETE"],
)
def test_non_get_methods_still_captured_on_polling_paths(method: str) -> None:
    """POST submit / DELETE remain visible in the inspector ring buffer
    so debugging mutations is unaffected by the GET exclusion."""
    assert _inspector_should_capture("/api/blast/jobs", method) is True
    assert _inspector_should_capture("/api/monitor/aks/run-command", method) is True


def test_sse_and_health_paths_excluded_for_all_methods() -> None:
    assert _inspector_should_capture("/api/monitor/sidecars", "GET") is False
    assert _inspector_should_capture("/api/monitor/sidecars/events", "GET") is False
    assert _inspector_should_capture("/api/blast/logs/job-1/events", "GET") is False
    assert _inspector_should_capture("/api/terminal/ws", "GET") is False
    assert _inspector_should_capture("/api/health", "GET") is False
    assert _inspector_should_record("/api/monitor/sidecars") is False
    assert _inspector_should_record("/api/monitor/sidecars/events") is False
    assert _inspector_should_record("/api/blast/logs/job-1/events") is False
    assert _inspector_should_record("/api/terminal/ws") is False
    assert _inspector_should_record("/api/health") is False
    # Backwards-compat — single-arg call sites treat path as non-GET and
    # should still hit the path-only excludes.
    assert _inspector_should_capture("/api/blast/logs/job-1/events") is False


def test_non_polling_get_still_captured() -> None:
    """GETs outside the polling prefix list must still be captured so the
    inspector remains useful for one-shot debugging GETs."""
    assert _inspector_should_capture("/api/arm/locations", "GET") is True
    assert _inspector_should_capture("/api/audit/recent", "GET") is True
