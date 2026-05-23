"""Compatibility wrapper for `api.services.warmup.jobs`.

Responsibility: Re-export `api.services.warmup.jobs` at the legacy flat path.
Edit boundaries: Real impl lives in `api.services.warmup.jobs`; do not add logic here.
Key entry points: Module import side effects and constants.
Risky contracts: Keep `__all__` in sync with the underlying module's public surface.
Validation: `uv run pytest -q api/tests/test_warmup_jobs.py`.
"""

from api.services.warmup.jobs import (
    WarmupJobPlan,
    attach_pod_progress_to_database_status,
    build_warmup_job_plan,
    build_warmup_scripts_configmap,
    database_status_from_warmup_jobs,
    infer_warmup_pod_phase,
)

__all__ = [
    "WarmupJobPlan",
    "attach_pod_progress_to_database_status",
    "build_warmup_job_plan",
    "build_warmup_scripts_configmap",
    "database_status_from_warmup_jobs",
    "infer_warmup_pod_phase",
]
