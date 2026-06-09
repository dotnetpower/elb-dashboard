"""Guard tests for the `api.tasks.*` package facade contracts.

Responsibility: Prove every `monkeypatch.setattr("api.tasks.<pkg>.<attr>", ...)`
    target used elsewhere in the suite resolves to a real attribute on the package
    facade. Without this, an SRP-style refactor can silently break the indirection
    contract — the test passes locally (because the patched path becomes a no-op when
    the attribute does not exist on the facade) but the production code path no longer
    consults the patched name and the regression goes unnoticed until the affected
    task actually runs in CI.
Edit boundaries: Pure introspection of the facades. When you add a new string-target
    monkeypatch in another test, append the target to `_FACADE_CONTRACT` here so the
    guard catches future drift.
Key entry points: `test_facade_contract_attributes_resolve`,
    `test_facade_contract_attributes_are_listed_in___all__`.
Risky contracts: `_FACADE_CONTRACT` must mirror the actual string-target monkeypatch
    surface — see `scripts/dev/audit_monkeypatch_targets.py` for the regenerator. The
    `.delay` suffix indicates a Celery task; tests resolve to the task object itself
    and rely on Celery providing `.delay` as a bound method.
Validation: `uv run pytest -q api/tests/test_tasks_facade_contract.py`.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

# Every `monkeypatch.setattr("api.tasks.X.Y", ...)` target used anywhere in
# the test suite. Keep alphabetical. When you add a new string-target patch,
# add it here too — the regenerator at the bottom of this docstring will
# print the canonical set.
_FACADE_CONTRACT: tuple[str, ...] = (
    "api.tasks.azure.assign_aks_roles",  # .delay
    "api.tasks.azure.idle_autostop._live_blast_signal",
    "api.tasks.azure.peering.delete_vnet_peering_on_cluster",
    "api.tasks.azure.peering.ensure_vnet_peering_with_cluster",
    "api.tasks.azure.peering.ensure_vnet_peering_with_target",
    "api.tasks.azure.peering.httpx.get",
    "api.tasks.azure.peering.list_vnet_peerings_for_cluster",
    "api.tasks.azure.peering_nsg.apply_inbound_allow_rule",
    "api.tasks.azure.peering_nsg.has_nsg_write_permission",
    "api.tasks.azure.peering_nsg.resolve_nsg_context",
    "api.tasks.azure.peering_nsg.resolve_vnet_pair_for_cluster",
    "api.tasks.azure.start_aks",  # .delay
    "api.tasks.azure.stop_aks.run",
    "api.tasks.blast.submit",  # .delay
    "api.tasks.blast.submit_task.persist_submit_log_events",
    "api.tasks.blast.submit_task.poll_running_status",
    "api.tasks.blast.submit_task.time.time",
    "api.tasks.blast.submit_task.upload_db_order_oracle_pointer_if_available",
    "api.tasks.blast.submit_task.upload_tie_order_oracle_if_present",
    "api.tasks.storage._autowarmup_inflight_acquire",
    "api.tasks.storage._record_task_progress",
    "api.tasks.storage._update_state",
    "api.tasks.storage.get_credential",
)


def _resolve(dotted_path: str) -> object:
    """Resolve a dotted attribute path the way `monkeypatch.setattr` does.

    `monkeypatch.setattr("api.tasks.azure.peering.httpx.get", …)` imports the
    deepest importable prefix (`api.tasks.azure.peering`) and then walks the
    remaining attributes (`httpx`, `get`). Mirror that so attribute paths that
    pass through a third-party namespace re-imported into a facade module still
    resolve from this guard.
    """
    parts = dotted_path.split(".")
    module = None
    last_imported = 0
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        try:
            module = importlib.import_module(candidate)
        except ImportError:
            continue
        last_imported = i
        break
    if module is None:
        raise ImportError(f"could not import any prefix of {dotted_path!r}")
    obj: object = module
    for attr in parts[last_imported:]:
        obj = getattr(obj, attr)
    return obj


@pytest.mark.parametrize("target", _FACADE_CONTRACT)
def test_facade_contract_attributes_resolve(target: str) -> None:
    """Every contract attribute must be importable from its facade."""
    obj = _resolve(target)
    assert obj is not None, f"{target} resolved to None — facade re-export missing?"


@pytest.mark.parametrize("target", _FACADE_CONTRACT)
def test_facade_contract_attributes_are_listed_in___all__(target: str) -> None:
    """Private (`_X`) attributes must appear in the facade's `__all__`.

    Without this, ``ruff --fix`` can delete the facade's `from ... import _x`
    line as an "unused import" — silently breaking the monkeypatch contract.
    Documented in repo memory (work-discipline note 2026-05-19 + tasks/*
    facade pattern 2026-05-22).
    """
    parent_path, _, attr = target.rpartition(".")
    if not attr.startswith("_"):
        # Public symbols do not need to be in __all__ for the import to
        # survive a refactor; the F401 risk is private-only.
        return
    module = importlib.import_module(parent_path)
    declared = set(getattr(module, "__all__", ()) or ())
    assert attr in declared, (
        f"{attr!r} is monkeypatched as a string target on {parent_path} but is "
        f"missing from {parent_path}.__all__. Add it so `ruff --fix` does not "
        "silently drop the facade re-export. See repo memory tasks/* facade "
        "pattern."
    )


def _iter_string_targets() -> Iterator[str]:
    pattern = re.compile(r'monkeypatch\.setattr\(\s*["\']([^"\']+)["\']')
    tests_dir = Path(__file__).parent
    for path in sorted(tests_dir.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        for match in pattern.finditer(path.read_text()):
            target = match.group(1)
            if target.startswith("api.tasks.") and not target.endswith(".delay"):
                yield target


def test_facade_contract_covers_all_string_target_monkeypatches() -> None:
    """`_FACADE_CONTRACT` must include every `api.tasks.*` string monkeypatch.

    Regenerator: replace `_FACADE_CONTRACT` with the printed tuple from
    ``uv run python -c "from api.tests.test_tasks_facade_contract import
    _iter_string_targets; print(sorted(set(_iter_string_targets())))"``.
    """
    discovered = set(_iter_string_targets())
    contract = {target for target in _FACADE_CONTRACT if not target.endswith(".delay")}
    # `.delay` targets describe Celery tasks; the rest are bare attrs.
    bare_contract = {target.rsplit(".delay", 1)[0] for target in _FACADE_CONTRACT}
    bare_contract.update(contract)
    missing = discovered - bare_contract
    assert not missing, (
        "These `monkeypatch.setattr(\"api.tasks.…\", …)` targets exist in the "
        f"test suite but are not in `_FACADE_CONTRACT`: {sorted(missing)}. "
        "Add them so refactors that drop them fail loudly here instead of "
        "silently breaking individual tests."
    )
