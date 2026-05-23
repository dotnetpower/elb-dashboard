"""Compatibility wrapper for `api.routes.terminal.legacy`.

Responsibility: Re-export the legacy-VM terminal router (410 Gone) at the flat path.
Edit boundaries: Real impl lives in `api.routes.terminal.legacy`; do not add logic here.
Key entry points: `router`.
Risky contracts: Endpoint must keep returning 410 — no new VM-based handlers.
Validation: `uv run pytest -q api/tests`.
"""

from api.routes.terminal.legacy import router

__all__ = ["router"]
