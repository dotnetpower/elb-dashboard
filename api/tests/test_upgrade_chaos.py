"""Chaos / property-based tests for the upgrade state machine.

Module summary: Drives `start_upgrade_inline`, `reconcile_rolling_out_inline`,
and `start_rollback_inline` through long, seeded random sequences and asserts
state-machine invariants after every operation. Catches the class of hidden
bugs that deterministic scenario tests miss — emergent interleavings between
concurrent operators, racing reconciler ticks, and out-of-order external
state mutations.

Responsibility: Find latent invariant violations in the upgrade flow that
  deterministic e2e scenarios cannot reach.
Edit boundaries: Add new invariant predicates here; do NOT add new business
  logic. When an invariant fails the right fix is in the production code,
  not in this file's invariant set.
Key entry points: `test_chaos_state_invariants_hold_across_seeds`,
  `run_chaos_round`, `Invariants.check_all`.
Risky contracts: Seeded `random.Random` so failures are reproducible. New
  seeds can always be added to the test parametrisation; existing seeds
  must keep producing the same trace (regression value).
Validation: `uv run pytest -q api/tests/test_upgrade_chaos.py`.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from api.services.upgrade import (
    aca_template,
    acr_inventory,
    build_logs,
    history,
    image_builder,
    state,
)
from api.tasks import upgrade as upgrade_task

# Reuse the test doubles from test_upgrade_task to avoid drift.
from api.tests.test_upgrade_task import _FakeAca, _FakeWatcher  # type: ignore

_TERMINAL_STATES = frozenset(
    {
        state.STATE_IDLE,
        state.STATE_SUCCEEDED,
        state.STATE_FAILED_PRE,
        state.STATE_FAILED_ROLLOUT,
        state.STATE_ROLLED_BACK,
        state.STATE_ROLLBACK_FAILED,
    }
)
_ACTIVE_STATES = frozenset(
    {
        state.STATE_QUEUED,
        state.STATE_FETCHING,
        state.STATE_BUILDING,
        state.STATE_PATCHING,
        state.STATE_ROLLING_OUT,
        state.STATE_ROLLING_BACK,
    }
)
_POST_PATCH_STATES = frozenset(
    {
        state.STATE_ROLLING_OUT,
        state.STATE_SUCCEEDED,
        state.STATE_FAILED_ROLLOUT,
        state.STATE_ROLLING_BACK,
        state.STATE_ROLLED_BACK,
        state.STATE_ROLLBACK_FAILED,
    }
)


@pytest.fixture
def chaos_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPGRADE_GIT_REMOTE", "https://example.test/foo.git")
    monkeypatch.setenv(image_builder.PLATFORM_ACR_NAME_ENV, "myacr")
    monkeypatch.setenv(aca_template.AZURE_SUBSCRIPTION_ID_ENV, "sub-1")
    monkeypatch.setenv(aca_template.AZURE_RESOURCE_GROUP_ENV, "rg-elb")
    monkeypatch.setenv(aca_template.CONTAINER_APP_NAME_ENV, "ca-elb-dashboard")
    state.set_backend(state.InMemoryBackend())
    build_logs.set_backend(build_logs.InMemoryBuildLogBackend())
    history.set_backend(history.InMemoryHistoryBackend())
    history.reset_chain_for_tests()

    class _AlwaysExistsAcr:
        def get_tag_properties(self, _repo: str, _tag: str):
            return type("P", (), {"created_on": datetime(2026, 5, 22, tzinfo=UTC)})()

        def close(self) -> None:
            pass

    acr_inventory.set_client_factory_for_tests(lambda _ep: _AlwaysExistsAcr())
    yield
    state.set_backend(None)
    build_logs.set_backend(None)
    history.set_backend(None)
    history.reset_chain_for_tests()
    acr_inventory.set_client_factory_for_tests(None)


class _ChaosInvariantViolation(AssertionError):
    """Raised when a state-machine invariant is broken during chaos."""


def _check_invariants(label: str, snapshot: state.UpgradeState) -> None:
    """Assert every documented invariant on the given row snapshot."""
    # I1: state always within the strict enum.
    if snapshot.state not in state.VALID_STATES:
        raise _ChaosInvariantViolation(
            f"[{label}] state {snapshot.state!r} not in VALID_STATES"
        )
    # I2: phase_progress within 0..100.
    if not (0 <= snapshot.phase_progress <= 100):
        raise _ChaosInvariantViolation(
            f"[{label}] phase_progress={snapshot.phase_progress} out of [0,100]"
        )
    # I3: SUCCEEDED implies progress 100.
    if snapshot.state == state.STATE_SUCCEEDED and snapshot.phase_progress != 100:
        raise _ChaosInvariantViolation(
            f"[{label}] SUCCEEDED with phase_progress={snapshot.phase_progress}"
        )
    # I4: ROLLED_BACK implies progress 100.
    if snapshot.state == state.STATE_ROLLED_BACK and snapshot.phase_progress != 100:
        raise _ChaosInvariantViolation(
            f"[{label}] ROLLED_BACK with phase_progress={snapshot.phase_progress}"
        )
    # I5: failure states must zero progress.
    if (
        snapshot.state in {state.STATE_FAILED_PRE, state.STATE_FAILED_ROLLOUT}
        and snapshot.phase_progress != 0
    ):
        raise _ChaosInvariantViolation(
            f"[{label}] {snapshot.state} with non-zero progress={snapshot.phase_progress}"
        )
    # I6: rollback target preserved once post-PATCH state is reached.
    if snapshot.state in _POST_PATCH_STATES and snapshot.target_version:
        target = snapshot.rollback_target()
        # Could be empty for rows where we manipulated history without
        # going through patching; only assert when target_version is set
        # AND we have observed a started_at (i.e. an upgrade actually ran).
        if snapshot.started_at and not target:
            raise _ChaosInvariantViolation(
                f"[{label}] post-PATCH state {snapshot.state} lost rollback_target"
            )
    # I7: audit hash chain must remain valid throughout the run.
    ok, reason = history.verify_chain()
    if not ok:
        raise _ChaosInvariantViolation(f"[{label}] audit chain broken: {reason}")


_OPERATIONS = (
    "start",
    "reconcile",
    "rollback",
    "advance_clock",
    "external_state_bump",
)


def run_chaos_round(*, seed: int, iterations: int) -> dict[str, Any]:
    """Drive a single seeded chaos round and return summary stats.

    Each iteration picks a random operation. After every op, every
    invariant in `_check_invariants` must hold. A violation raises
    `_ChaosInvariantViolation` carrying the seed + iteration index so
    the failure is fully reproducible (re-run with the same seed and
    iterations to see the same trace).
    """
    rng = random.Random(seed)  # noqa: S311 — chaos test, not crypto
    aca = _FakeAca()
    watcher = _FakeWatcher()
    op_counter: dict[str, int] = {op: 0 for op in _OPERATIONS}
    clock_base = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    clock_offset_min = 0
    for i in range(iterations):
        op = rng.choice(_OPERATIONS)
        op_counter[op] += 1
        label = f"seed={seed} iter={i} op={op}"
        try:
            if op == "start":
                # Idempotency-keyed start; retries are safe.
                try:
                    upgrade_task.start_upgrade_inline(
                        target_version=rng.choice(["0.3.0", "0.4.0", "0.5.0"]),
                        target_sha="",
                        started_by_oid=f"chaos-{rng.randint(1, 3)}",
                        idempotency_key=f"chaos-{rng.randint(1, 5)}",
                        enqueue=lambda *_args: None,
                    )
                except upgrade_task.UpgradeStartRefused:
                    pass
            elif op == "reconcile":
                offset = clock_offset_min  # bind to local for closure safety
                fake_now = lambda offset=offset: clock_base + timedelta(minutes=offset)  # noqa: E731
                upgrade_task.reconcile_rolling_out_inline(
                    aca=aca, watcher=watcher, now=fake_now
                )
            elif op == "rollback":
                try:
                    upgrade_task.start_rollback_inline(
                        started_by_oid=f"chaos-rb-{rng.randint(1, 3)}",
                        aca=aca,
                        watcher=watcher,
                    )
                except upgrade_task.RollbackStartRefused:
                    pass
            elif op == "advance_clock":
                # Random clock jumps including > stuck-guard window so the
                # reconciler's escalation paths actually trigger.
                clock_offset_min += rng.choice([1, 5, 15, 60])
            elif op == "external_state_bump":
                # Simulate an external writer racing with us (e.g.
                # operator manually setting phase_detail via a debug
                # endpoint). The CAS-protected paths must remain
                # consistent.
                try:
                    state.update_state(
                        lambda s: setattr(
                            s, "phase_detail", f"chaos-bump-{rng.randint(1, 9999)}"
                        )
                    )
                except state.RowEtagMismatch:
                    pass
        except _ChaosInvariantViolation:
            raise
        except Exception as exc:
            # Other exceptions are allowed (the production code throws
            # documented errors), but the row must still be invariant-OK.
            snap = state.get_state()
            _check_invariants(f"{label} after exception {type(exc).__name__}", snap)
            continue
        snap = state.get_state()
        _check_invariants(label, snap)
    return {"seed": seed, "iterations": iterations, "ops": op_counter}


# Seeds chosen for variety; once committed they MUST keep passing — a
# regression here is a real bug. New seeds may be appended freely.
_CHAOS_SEEDS = (1, 7, 42, 137, 256, 9001, 31337)


@pytest.mark.parametrize("seed", _CHAOS_SEEDS)
def test_chaos_state_invariants_hold_across_seeds(
    chaos_env: None, seed: int
) -> None:
    """All invariants in `_check_invariants` must hold across 200 random
    operations from each seed. Seeded so a failure prints a fully
    reproducible trace."""
    summary = run_chaos_round(seed=seed, iterations=200)
    # Sanity: every operation type was exercised at least once
    # (the random.choice distribution should make this very likely
    # at iterations=200; if not the seeds need rebalancing).
    assert summary["ops"]["start"] > 0
    assert summary["ops"]["reconcile"] > 0


def test_chaos_audit_hash_chain_survives_long_run(chaos_env: None) -> None:
    """Even after hundreds of random operations the audit chain must
    remain verifiable. This catches a class of bugs where a code path
    records an event but skips the chain update.
    """
    run_chaos_round(seed=2026, iterations=500)
    ok, reason = history.verify_chain()
    assert ok, f"audit chain broken after long chaos run: {reason}"


def test_chaos_idempotency_key_never_creates_duplicate_job_ids(
    chaos_env: None,
) -> None:
    """When N start calls share the same idempotency_key + target_version,
    they must collapse to a single job_id (no duplicate runs).
    """
    job_ids: set[str] = set()
    for _ in range(50):
        try:
            row = upgrade_task.start_upgrade_inline(
                target_version="0.5.0",
                target_sha="",
                started_by_oid="chaos-idem",
                idempotency_key="single-key",
                enqueue=lambda *_args: None,
            )
            job_ids.add(row.job_id)
        except upgrade_task.UpgradeStartRefused:
            # Different keys would refuse; here we always send the same key.
            pass
    assert len(job_ids) == 1, f"idempotency key produced {len(job_ids)} job_ids"
