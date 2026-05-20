"""Compatibility wrapper for `api.services.k8s.nodes`."""

from api.services.k8s.nodes import (
    _candidate_warmup_node_names,
    k8s_get_nodes,
    k8s_ready_warmup_node_names,
)

__all__ = [
    "_candidate_warmup_node_names",
    "k8s_get_nodes",
    "k8s_ready_warmup_node_names",
]
