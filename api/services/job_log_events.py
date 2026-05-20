"""Compatibility facade for live BLAST job log helpers."""

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
