"""Compatibility wrapper for `api.routes.terminal.ws`.

Responsibility: Re-export the WebSocket terminal router at the legacy flat path.
Edit boundaries: Real impl lives in `api.routes.terminal.ws`; do not add logic here.
Key entry points: `router`.
Risky contracts: Same as the real module — bearer-validate before upgrade.
Validation: `uv run pytest -q api/tests/test_terminal_ws_origin.py`.
"""

from api.routes.terminal.ws import router

__all__ = ["router"]
