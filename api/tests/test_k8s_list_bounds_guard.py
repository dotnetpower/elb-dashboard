"""Guard: cluster-wide Kubernetes LIST calls must stay bounded.

Responsibility: Fail the build if a cluster-wide (namespace-less) Kubernetes
    LIST is issued without paging. That exact pattern made api memory
    O(cluster object count) and OOM-killed the sidecar with exit 137 — 56 times
    on 2026-07-31, and zero on 2026-08-04 when the cluster happened to be
    stopped. A source-level guard is the only thing that stops it coming back
    the next time someone adds a workload tab.
Edit boundaries: Static source inspection only. Behavioural coverage of the
    paging loop itself lives in `test_k8s_paged_list.py`.
Key entry points: `test_cluster_wide_lists_go_through_the_paged_helper`.
Risky contracts: When a new cluster-wide LIST is genuinely bounded some other
    way, add it to `_ALLOWED_UNPAGED` **with the reason** rather than deleting
    the guard.
Validation: ``uv run pytest -q api/tests/test_k8s_list_bounds_guard.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

_K8S_DIR = Path(__file__).resolve().parents[1] / "services" / "k8s"

# Cluster-wide collection endpoints only. The group-version pattern is written
# so a namespaced URL (`/apis/batch/v1/namespaces/default/jobs`) can NOT match:
# after the version segment the next path element must be the collection name.
_CLUSTER_WIDE_LIST = re.compile(
    r'f"\{server\}(/api/v1|/apis/[a-z0-9.]+/v[0-9a-z]+)'
    r'/(pods|jobs|deployments|events|nodes|configmaps)"'
)

# Endpoints that are bounded without the paged helper, with the reason. Adding
# an entry here is a deliberate, reviewable act — deleting the guard is not.
_ALLOWED_UNPAGED: dict[str, str] = {
    # Node counts are bounded by the AKS node pool max (tens), not by workload
    # churn, and every node object is small.
    "nodes.py:/api/v1/nodes": "bounded by node-pool size",
    "metrics.py:/api/v1/nodes": "bounded by node-pool size",
    "warmup_status.py:/api/v1/nodes": "bounded by node-pool size",
    "node_pressure.py:/api/v1/nodes": "bounded by node-pool size",
    "metrics.py:/apis/metrics.k8s.io/v1beta1/nodes": "bounded by node-pool size",
    # metrics.k8s.io does not implement limit/continue; each PodMetrics object is
    # a name plus per-container usage (~200 B), so the body stays small.
    "metrics.py:/apis/metrics.k8s.io/v1beta1/pods": (
        "aggregated API, tiny objects, no paging support"
    ),
    # k8s_list_events already passes an explicit `limit` param of its own.
    "observability.py:/api/v1/events": "explicit limit param in k8s_list_events",
}


def _iter_cluster_wide_lists() -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for path in sorted(_K8S_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in _CLUSTER_WIDE_LIST.finditer(text):
            endpoint = f"{match.group(1)}/{match.group(2)}"
            found.append((path.name, endpoint, text))
    return found


def test_guard_actually_finds_the_known_cluster_wide_lists() -> None:
    """Sanity-check the regex — a guard that matches nothing guards nothing."""
    endpoints = {f"{name}:{endpoint}" for name, endpoint, _ in _iter_cluster_wide_lists()}

    assert "monitoring.py:/api/v1/pods" in endpoints
    assert "monitoring.py:/apis/apps/v1/deployments" in endpoints
    assert "monitoring.py:/apis/batch/v1/jobs" in endpoints


def test_guard_does_not_flag_namespaced_lists() -> None:
    """A namespaced LIST is bounded by that namespace and is out of scope."""
    endpoints = {f"{name}:{endpoint}" for name, endpoint, _ in _iter_cluster_wide_lists()}

    assert not any("namespaces" in endpoint for endpoint in endpoints)


def test_cluster_wide_lists_go_through_the_paged_helper() -> None:
    offenders: list[str] = []
    for name, endpoint, text in _iter_cluster_wide_lists():
        key = f"{name}:{endpoint}"
        if key in _ALLOWED_UNPAGED:
            continue
        if "list_k8s_items" not in text:
            offenders.append(key)

    assert not offenders, (
        "cluster-wide Kubernetes LIST without paging: "
        + ", ".join(offenders)
        + " — route it through api.services.k8s.paged_list.list_k8s_items, or add it "
        "to _ALLOWED_UNPAGED with the reason it is already bounded."
    )
