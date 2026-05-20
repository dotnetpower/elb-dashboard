"""Small shared constants for /api/blast route modules.

Responsibility: Small shared constants for /api/blast route modules
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: Module import side effects and constants.
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_blast_results_routes.py
api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

LAB_TOOL_PENDING = {
    "code": "lab_tool_backend_pending",
    "message": "This Lab Tool route has no backend implementation in the Container Apps build yet.",
}
