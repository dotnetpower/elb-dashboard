"""Guard tests for the `api.services.*` flat compatibility shims.

Responsibility: Prove every flat `api/services/<prefix>_<name>.py` shim left
behind by Phase A still resolves to the canonical `api.services.<prefix>.<name>`
module, and that public symbols listed in the shim's ``__all__`` round-trip
identically to the real module's attributes. Without this, a future SRP refactor
can silently break the shim contract (the shim still imports cleanly but no
longer forwards new symbols) and the regression goes unnoticed until a caller
hits the missing name.
Edit boundaries: Pure introspection. When a new shim is added or removed, update
``_FLAT_SHIMS`` below. The test does not assert on private symbols (`_X`) — the
shim is free to drop them as long as no string-target monkeypatch refers to them.
Key entry points: ``test_flat_shim_resolves_to_subpackage``,
``test_flat_shim_all_round_trips``, ``test_flat_shim_getattr_proxy_round_trips``.
Risky contracts: ``_FLAT_SHIMS`` must mirror the on-disk shim files. The
``test_no_unlisted_flat_shim`` guard scans the directory and fails if a new
shim sneaks in without being registered here.
Validation: ``uv run pytest -q api/tests/test_services_facade_contract.py``.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest

# (flat_shim_path, real_subpackage_module_path) pairs.
# Keep alphabetical within each domain so additions are easy to spot.
_FLAT_SHIMS: tuple[tuple[str, str], ...] = (
    # blast/
    ("api.services.blast_compatibility", "api.services.blast.compatibility"),
    ("api.services.blast_config", "api.services.blast.config"),
    ("api.services.blast_db_metadata", "api.services.blast.db_metadata"),
    ("api.services.blast_equivalence_evidence", "api.services.blast.equivalence_evidence"),
    ("api.services.blast_events", "api.services.blast.events"),
    ("api.services.blast_external_jobs", "api.services.blast.external_jobs"),
    ("api.services.blast_job_state", "api.services.blast.job_state"),
    ("api.services.blast_oracles", "api.services.blast.oracles"),
    ("api.services.blast_provenance", "api.services.blast.provenance"),
    ("api.services.blast_queue", "api.services.blast.queue"),
    ("api.services.blast_result_analytics", "api.services.blast.result_analytics"),
    ("api.services.blast_result_artifacts", "api.services.blast.result_artifacts"),
    ("api.services.blast_result_manifest", "api.services.blast.result_manifest"),
    ("api.services.blast_results_parser", "api.services.blast.results_parser"),
    ("api.services.blast_submit_payload", "api.services.blast.submit_payload"),
    ("api.services.blast_submit_gates", "api.services.blast.submit_gates"),
    ("api.services.blast_task_config", "api.services.blast.task_config"),
    # db/
    ("api.services.db_ops_audit", "api.services.db.ops_audit"),
    ("api.services.db_order_oracle", "api.services.db.order_oracle"),
    ("api.services.db_sharding", "api.services.db.sharding"),
    # k8s/
    ("api.services.k8s_client", "api.services.k8s.client"),
    ("api.services.k8s_credentials", "api.services.k8s.credentials"),
    ("api.services.k8s_fanout", "api.services.k8s.fanout"),
    ("api.services.k8s_ingress", "api.services.k8s.ingress"),
    ("api.services.k8s_manifests", "api.services.k8s.manifests"),
    ("api.services.k8s_metrics", "api.services.k8s.metrics"),
    ("api.services.k8s_monitoring", "api.services.k8s.monitoring"),
    ("api.services.k8s_nodes", "api.services.k8s.nodes"),
    ("api.services.k8s_observability", "api.services.k8s.observability"),
    ("api.services.k8s_timestamps", "api.services.k8s.timestamps"),
    # openapi/
    ("api.services.openapi_deployment", "api.services.openapi.deployment"),
    ("api.services.openapi_runtime", "api.services.openapi.runtime"),
    ("api.services.openapi_token", "api.services.openapi.token"),
    # storage/
    ("api.services.storage_data", "api.services.storage.data"),
    ("api.services.storage_blob_ids", "api.services.storage.blob_ids"),
    ("api.services.storage_blob_io", "api.services.storage.blob_io"),
    ("api.services.storage_blob_paths", "api.services.storage.blob_paths"),
    ("api.services.storage_client_pool", "api.services.storage.client_pool"),
    ("api.services.storage_database_list", "api.services.storage.database_list"),
    ("api.services.storage_endpoint", "api.services.storage.endpoint"),
    ("api.services.storage_failure_classifier", "api.services.storage.failure_classifier"),
    ("api.services.storage_local_rbac", "api.services.storage.local_rbac"),
    ("api.services.storage_network", "api.services.storage.network"),
    ("api.services.storage_public_access", "api.services.storage.public_access"),
    ("api.services.storage_usage", "api.services.storage.usage"),
    ("api.services.storage_url_validation", "api.services.storage.url_validation"),
    ("api.services.storage_usage_cache", "api.services.storage.usage_cache"),
    # warmup/
    ("api.services.warmup_jobs", "api.services.warmup.jobs"),
    ("api.services.warmup_planner", "api.services.warmup.planner"),
    ("api.services.warmup_scripts", "api.services.warmup.scripts"),
    ("api.services.warmup_task_planning", "api.services.warmup.task_planning"),
)


_FLAT_PREFIXES: tuple[str, ...] = (
    "blast_",
    "db_",
    "k8s_",
    "openapi_",
    "storage_",
    "warmup_",
)


@pytest.mark.parametrize("shim_path,real_path", _FLAT_SHIMS)
def test_flat_shim_resolves_to_subpackage(shim_path: str, real_path: str) -> None:
    """Both the shim and the real subpackage module must be importable."""
    shim = importlib.import_module(shim_path)
    real = importlib.import_module(real_path)
    assert shim is not None
    assert real is not None
    # The shim's __name__ must remain the legacy path so existing
    # `sys.modules` lookups and `__module__` introspection keep working.
    assert shim.__name__ == shim_path


@pytest.mark.parametrize("shim_path,real_path", _FLAT_SHIMS)
def test_flat_shim_all_round_trips(shim_path: str, real_path: str) -> None:
    """If the shim declares `__all__`, every listed name must resolve to the
    same object on both modules.

    Catches the failure mode where the real module renames or drops a symbol
    listed in the shim — the shim import then raises ImportError on the next
    deploy.
    """
    shim = importlib.import_module(shim_path)
    real = importlib.import_module(real_path)
    declared = list(getattr(shim, "__all__", ()) or ())
    if not declared:
        # __getattr__-proxy shims have an empty/missing __all__ on purpose;
        # they are exercised by ``test_flat_shim_getattr_proxy_round_trips``.
        return
    for name in declared:
        assert hasattr(real, name), (
            f"{shim_path}.__all__ lists {name!r} but {real_path} does not export it; "
            f"either re-add the symbol to the real module or drop it from the shim's __all__."
        )
        assert getattr(shim, name) is getattr(real, name), (
            f"{shim_path}.{name} is not the same object as {real_path}.{name}; "
            "the shim is shadowing the real symbol."
        )


@pytest.mark.parametrize("shim_path,real_path", _FLAT_SHIMS)
def test_flat_shim_getattr_proxy_round_trips(shim_path: str, real_path: str) -> None:
    """For shims that use a module-level `__getattr__` proxy (empty `__all__`),
    arbitrary attribute access on the shim must forward to the real module.

    Sampled with one well-known attribute per module — we check whichever
    of `__doc__`, `__file__`, or `__name__` is present (all three are always
    set on a real module, so this is a smoke test for the proxy wiring).
    """
    shim = importlib.import_module(shim_path)
    real = importlib.import_module(real_path)
    declared = list(getattr(shim, "__all__", ()) or ())
    if declared:
        # Explicit-__all__ shims are covered by the round-trip test above.
        return
    # Module dunders should still resolve through the proxy.
    assert getattr(shim, "__doc__", None) is not None or real.__doc__ is None, (
        f"{shim_path} __getattr__ did not forward __doc__"
    )
    # Pick the first non-underscore public attribute of the real module and
    # verify identity round-trip via the shim.
    public_names = [n for n in dir(real) if not n.startswith("_")]
    sample = next(iter(public_names), None)
    if sample is None:
        return  # extremely small module — nothing to forward
    assert getattr(shim, sample) is getattr(real, sample), (
        f"{shim_path}.{sample} did not forward to {real_path}.{sample}"
    )


def test_no_unlisted_flat_shim() -> None:
    """Every `api/services/<prefix>_*.py` file on disk must be registered above.

    Otherwise a new shim added by a future refactor would not get the
    round-trip guard.
    """
    services_dir = Path(__file__).resolve().parent.parent / "services"
    on_disk: set[str] = set()
    for entry in services_dir.iterdir():
        if not entry.is_file() or entry.suffix != ".py":
            continue
        name = entry.stem
        if not any(name.startswith(p) for p in _FLAT_PREFIXES):
            continue
        on_disk.add(f"api.services.{name}")
    registered = {shim for shim, _ in _FLAT_SHIMS}
    missing = on_disk - registered
    assert not missing, (
        f"flat shims on disk but not registered in _FLAT_SHIMS: {sorted(missing)}. "
        "Add the (shim, real) pair so the contract test covers it."
    )


def test_flat_shim_count_matches_packages() -> None:
    """Sanity: there is a flat shim for every public submodule in the new
    subpackages (or the missing submodule was intentionally not promoted).
    """
    domains = ("blast", "db", "k8s", "openapi", "storage", "warmup")
    listed_real = {real for _, real in _FLAT_SHIMS}
    for domain in domains:
        pkg = importlib.import_module(f"api.services.{domain}")
        for _, name, ispkg in pkgutil.iter_modules(pkg.__path__):
            if ispkg:
                continue
            module_path = f"api.services.{domain}.{name}"
            assert module_path in listed_real, (
                f"{module_path} has no flat-shim entry in _FLAT_SHIMS — either "
                "add a shim at the legacy path or remove this assertion for the "
                "intentionally-new module."
            )
