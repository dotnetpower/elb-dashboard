"""pytest configuration for api/."""

import sys
from pathlib import Path

import pytest

# Make api/ importable as `api`.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _reset_external_jobs_cache():
    """Clear the in-memory external-OpenAPI jobs cache between every test.

    Without this, a test that mocks ``external_blast.list_jobs`` with one
    response can leak that response into a subsequent test whose mock
    expects to be the only source of truth.
    """
    from api.routes._blast_shared import _reset_external_jobs_cache as _reset
    from api.routes.blast.jobs import _reset_blast_jobs_list_cache

    _reset()
    _reset_blast_jobs_list_cache()
    yield
    _reset()
    _reset_blast_jobs_list_cache()
