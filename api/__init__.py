"""FastAPI control-plane API for ElasticBLAST on Azure.

This package backs the `api` sidecar (and the worker/beat sidecars, which
share the same image and import `api.celery_app`) in the bundled
Container App `ca-elb-control`. The legacy Azure Functions backend has
been retired and lives under `legacy/functionapp/` for reference only.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.0.1"
