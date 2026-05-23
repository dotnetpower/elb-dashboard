"""Browser-terminal route modules (WebSocket + legacy VM endpoints).

Responsibility: Group the terminal-related FastAPI routes under one namespace.
Edit boundaries: Submodules own their routers; this package only aggregates.
Key entry points: `ws.router` (xterm.js ↔ ttyd proxy), `legacy.router` (410 Gone
for the retired VM-based endpoints).
Risky contracts: The WS handshake must validate the MSAL bearer before upgrade.
Validation: `uv run pytest -q api/tests/test_terminal_ws_origin.py`.
"""

from __future__ import annotations

__all__: list[str] = []
