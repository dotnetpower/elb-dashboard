"""Celery task modules for the elb-dashboard control plane.

Each module is auto-discovered by the Celery worker via the ``include``
list in ``api.celery_app``.

Importing ``api.celery_app`` BEFORE the shared_task modules is load-bearing:
the api sidecar imports `api.tasks.<x>` lazily inside route handlers, and
without this guard `current_app` would resolve to a phantom default Celery
app (broker=amqp://, queue="celery", routes={}) — the produced messages
would land in a queue the worker doesn't subscribe to and tasks would
silently never run.
"""

from __future__ import annotations

# Ensure our Celery instance is set as default + current BEFORE any
# `@shared_task` decorator runs. Do not remove or reorder.
from api import celery_app as _celery_app  # noqa: F401

# Import task modules so Celery auto-discovers @shared_task decorators.
from api.tasks import acr, azure, blast, blast_artifacts, storage

__all__ = ["acr", "azure", "blast", "blast_artifacts", "storage"]
