"""AKS-adjacent Azure network reconcilers used by the control plane tasks.

Responsibility: Namespace package for reusable AKS node-networking helpers
    (e.g. ensuring the BYO node-subnet NSG permits the ingress LoadBalancer
    inbound path) that live above the raw `azure.mgmt.network` SDK.
Edit boundaries: Put reusable Azure network domain logic here; routes and
    Celery tasks call this layer instead of importing `azure.mgmt.*` directly.
Key entry points: submodules under `api.services.aks`.
Risky contracts: None — this `__init__` only marks the package.
Validation: `uv run pytest -q api/tests/test_node_subnet_nsg.py`.
"""

from __future__ import annotations
