"""Live BLAST job log service package.

Responsibility: Live BLAST job log service package
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `__all__`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from api.services.job_logs.event_bus import publish_job_log_event, read_job_log_events
from api.services.job_logs.k8s import (
    K8sLogTarget,
    discover_k8s_log_targets,
    elastic_blast_suffix,
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
