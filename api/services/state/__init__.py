"""Azure Table-backed job-state subpackage (split from `services/state_repo.py`).

Responsibility: Group the table pool, JobState dataclass, and repository class
under one namespace so each module owns a single responsibility.
Edit boundaries: Submodules own their behaviour; this package only aggregates.
Key entry points: `table_pool`, `job_state`, `repository` submodules.
Risky contracts: Keep Azure credentials centralized and sanitise data before
HTTP/log boundaries.
Validation: `uv run pytest -q api/tests/test_state_repo.py`.
"""

from __future__ import annotations

__all__: list[str] = []
