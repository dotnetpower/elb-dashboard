"""Compatibility wrapper for `api.services.warmup.scripts`.

Responsibility: Re-export `api.services.warmup.scripts` at the legacy flat path.
Edit boundaries: Real impl lives in `api.services.warmup.scripts`; do not add logic here.
Key entry points: Module import side effects and constants.
Risky contracts: Keep `__all__` in sync with the underlying module's public surface.
Validation: `uv run pytest -q api/tests/test_warmup_jobs.py`.
"""

from api.services.warmup.scripts import warmup_shell_command

__all__ = [
    "warmup_shell_command",
]
