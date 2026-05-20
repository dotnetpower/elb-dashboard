"""Compatibility facade for live BLAST job log helpers.

Responsibility: Compatibility facade for live BLAST job log helpers
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: Module import side effects and constants.
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from api.services.job_logs import (
    K8sLogTarget,
    discover_k8s_log_targets,
    elastic_blast_suffix,
    publish_job_log_event,
    read_job_log_events,
    stream_k8s_log_lines,
)

__all__ = [
    "K8sLogTarget",
    "discover_k8s_log_targets",
    "elastic_blast_suffix",
    "publish_job_log_event",
    "read_job_log_events",
    "stream_k8s_log_lines",
]
