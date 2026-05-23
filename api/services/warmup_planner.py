"""Compatibility wrapper for `api.services.warmup.planner`.

Responsibility: Re-export `api.services.warmup.planner` at the legacy flat path.
Edit boundaries: Real impl lives in `api.services.warmup.planner`; do not add logic here.
Key entry points: Module import side effects and constants.
Risky contracts: Keep `__all__` in sync with the underlying module's public surface.
Validation: `uv run pytest -q api/tests/test_warmup_planner.py`.
"""

from api.services.warmup.planner import (
    WarmupPlan,
    compute_warmup_feasibility,
)

__all__ = [
    "WarmupPlan",
    "compute_warmup_feasibility",
]
