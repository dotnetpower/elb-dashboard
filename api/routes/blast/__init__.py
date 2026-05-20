"""/api/blast/*`` route package.

Responsibility: /api/blast/*`` route package
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `package imports`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_blast_results_routes.py
api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.routes._blast_shared import (
    _config_preview_from_payload as _config_preview_from_payload,
)
from api.routes._blast_shared import (
    _openapi_client_kwargs_from_cluster as _openapi_client_kwargs_from_cluster,
)
from api.routes._blast_shared import (
    _safe_delay as _safe_delay,
)
from api.routes.blast import databases as _databases_routes
from api.routes.blast import jobs as _jobs_routes
from api.routes.blast import logs as _logs_routes
from api.routes.blast import preflight as _preflight_routes
from api.routes.blast import result_analytics as _result_analytics_routes
from api.routes.blast import results as _results_routes
from api.routes.blast import schedules as _schedules_routes
from api.routes.blast import submit as _submit_routes
from api.routes.blast import taxonomy as _taxonomy_routes
from api.routes.blast.databases import (
    blast_database_order_oracle as blast_database_order_oracle,
)
from api.routes.blast.databases import (
    blast_database_shard as blast_database_shard,
)
from api.routes.blast.databases import (
    blast_databases as blast_databases,
)
from api.routes.blast.databases import (
    blast_databases_build_stub as blast_databases_build_stub,
)
from api.routes.blast.databases import (
    blast_databases_check_updates as blast_databases_check_updates,
)
from api.routes.blast.databases import (
    blast_databases_versions as blast_databases_versions,
)
from api.routes.blast.jobs import (
    blast_job_cancel as blast_job_cancel,
)
from api.routes.blast.jobs import (
    blast_job_delete as blast_job_delete,
)
from api.routes.blast.jobs import (
    blast_job_events as blast_job_events,
)
from api.routes.blast.jobs import (
    blast_job_get as blast_job_get,
)
from api.routes.blast.jobs import (
    blast_job_queue as blast_job_queue,
)
from api.routes.blast.jobs import (
    blast_jobs_list as blast_jobs_list,
)
from api.routes.blast.preflight import blast_pre_flight as blast_pre_flight
from api.routes.blast.result_analytics import (
    blast_job_results_alignments as blast_job_results_alignments,
)
from api.routes.blast.result_analytics import (
    blast_job_results_taxonomy as blast_job_results_taxonomy,
)
from api.routes.blast.results import (
    blast_job_file as blast_job_file,
)
from api.routes.blast.results import (
    blast_job_result_file as blast_job_result_file,
)
from api.routes.blast.results import (
    blast_job_results as blast_job_results,
)
from api.routes.blast.results import (
    blast_job_results_aggregate as blast_job_results_aggregate,
)
from api.routes.blast.results import (
    blast_job_results_download as blast_job_results_download,
)
from api.routes.blast.results import (
    blast_job_results_export as blast_job_results_export,
)
from api.routes.blast.schedules import (
    blast_schedules_create as blast_schedules_create,
)
from api.routes.blast.schedules import (
    blast_schedules_delete as blast_schedules_delete,
)
from api.routes.blast.schedules import (
    blast_schedules_list as blast_schedules_list,
)
from api.routes.blast.schedules import (
    blast_schedules_run as blast_schedules_run,
)
from api.routes.blast.submit import (
    blast_cost_estimate_stub as blast_cost_estimate_stub,
)
from api.routes.blast.submit import (
    blast_job_submit as blast_job_submit,
)
from api.routes.blast.submit import (
    blast_preprocess_stub as blast_preprocess_stub,
)
from api.routes.blast.submit import (
    blast_primer_design_stub as blast_primer_design_stub,
)
from api.routes.blast.submit import (
    blast_submit as blast_submit,
)
from api.routes.blast.submit import (
    blast_submit_status as blast_submit_status,
)
from api.routes.blast.submit import (
    blast_upload_query as blast_upload_query,
)
from api.routes.blast.taxonomy import (
    blast_taxonomy_detail as blast_taxonomy_detail,
)
from api.routes.blast.taxonomy import (
    blast_taxonomy_image as blast_taxonomy_image,
)
from api.routes.blast.taxonomy import (
    blast_taxonomy_search as blast_taxonomy_search,
)
from api.routes.blast.taxonomy import (
    blast_taxonomy_stub as blast_taxonomy_stub,
)
from api.routes.blast.taxonomy import (
    blast_taxonomy_tree as blast_taxonomy_tree,
)

blast_router = APIRouter(prefix="/api/blast", tags=["blast"])
blast_router.include_router(_jobs_routes.router)
blast_router.include_router(_logs_routes.router)
blast_router.include_router(_preflight_routes.router)
blast_router.include_router(_submit_routes.router)
blast_router.include_router(_databases_routes.router)
blast_router.include_router(_taxonomy_routes.router)
blast_router.include_router(_schedules_routes.router)
blast_router.include_router(_result_analytics_routes.router)
blast_router.include_router(_results_routes.router)
