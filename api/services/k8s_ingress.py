"""Compatibility wrapper for `api.services.k8s.ingress`.

Responsibility: Re-export `api.services.k8s.ingress` at the legacy flat path.
Edit boundaries: Real impl lives in `api.services.k8s.ingress`; do not add logic here.
Key entry points: Module-level `__getattr__` forwards everything for back-compat.
Risky contracts: Keep the pinned `INGRESS_NGINX_INSTALL_URL` and
`CERT_MANAGER_INSTALL_URL` in the real module; this shim never overrides them.
Validation: `uv run pytest -q api/tests/test_openapi_public_https.py
api/tests/test_services_facade_contract.py`.
"""

from typing import Any

from api.services.k8s import ingress as _impl


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return dir(_impl)
