"""FastAPI control-plane API for ElasticBLAST on Azure.

This package is the future home of the `api` sidecar in the bundled Container
App. It is being introduced as a side-by-side scaffold while the existing
Azure Functions backend (`api/`) keeps serving production.

Phase 0 scope (this PR):
    * /api/health  — liveness + revision identity.
    * /api/me      — caller identity, derived from MSAL bearer token.
    * /api/monitor/cluster — stub endpoint demonstrating the auth-protected
      monitor-style route shape.

Phases 2-5 will add the streaming upload/download proxy, the WebSocket
terminal proxy, the catch-all reverse proxy to the frontend sidecar, the
Celery task dispatch surface, and the migrated route set.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.0.1"
