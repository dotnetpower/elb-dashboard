"""Compatibility wrapper for `api.services.k8s.nodes`.

Responsibility: Compatibility wrapper for `api.services.k8s.nodes`
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: Module import side effects and constants.
Risky contracts: Use direct Kubernetes API helpers; do not reintroduce Azure Run Command.
Validation: `uv run pytest -q api/tests/test_k8s_list_events.py`.
"""

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
