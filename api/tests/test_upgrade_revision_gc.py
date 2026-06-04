"""Tests for the keep-N revision + orphan ACR tag garbage collector.

Module summary: Drives `collect_garbage_inline` with fake revisions /
ACR modules and an in-memory state row to assert the retain policy and
the rollback-protecting guards.

Responsibility: Verify GC deactivates only stale revisions, never the
  protected (serving + blue/green) set, and deletes only tags no
  retained revision references.
Edit boundaries: Update when the keep-N policy or the protected-set
  contract changes.
Key entry points: Tests for keep-N retention, protected-set, orphan tag
  deletion, best-effort failure isolation.
Risky contracts: Asserts the serving revision and the row's blue/green
  are never deactivated (the instant-rollback invariant).
Validation: `uv run pytest -q api/tests/test_upgrade_revision_gc.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from api.services.upgrade import aca_template, state
from api.tasks.upgrade import revision_gc


@dataclass
class _Rev:
    name: str
    active: bool
    weight: int
    created_on: datetime | None
    label: str = ""
    running_state: str = "Running"
    provisioning_state: str = "Provisioned"


class _FakeRevisions:
    def __init__(
        self,
        revs: list[_Rev],
        serving: str,
        images: dict[str, set[str]],
    ) -> None:
        self._revs = revs
        self._serving = serving
        self._images = images
        self.deactivated: list[str] = []
        self.deactivate_fail: set[str] = set()

    def list_revisions(self) -> list[_Rev]:
        return self._revs

    def serving_revision(self) -> str:
        return self._serving

    def revision_image_refs(self) -> dict[str, set[str]]:
        return self._images

    def deactivate_revision(self, *, revision_name: str) -> bool:
        if revision_name in self.deactivate_fail:
            return False
        self.deactivated.append(revision_name)
        return True


class _FakeAcr:
    def __init__(self, deletable: set[str] | None = None) -> None:
        self.deletable = deletable
        self.delete_calls: list[str] = []

    def delete_tag_best_effort(self, image_ref: str) -> tuple[bool, str]:
        self.delete_calls.append(image_ref)
        if self.deletable is not None and image_ref not in self.deletable:
            return False, "not-deletable"
        return True, "deleted"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(aca_template.AZURE_SUBSCRIPTION_ID_ENV, "sub-1")
    monkeypatch.setenv(aca_template.AZURE_RESOURCE_GROUP_ENV, "rg-elb")
    monkeypatch.setenv(aca_template.CONTAINER_APP_NAME_ENV, "ca-elb-dashboard")
    monkeypatch.setenv(aca_template.PLATFORM_ACR_NAME_ENV, "myacr")
    state.set_backend(state.InMemoryBackend())
    yield
    state.set_backend(None)


def _now() -> datetime:
    return datetime.now(UTC)


def test_keep_n_retains_serving_plus_recent() -> None:
    now = _now()
    revs = [
        _Rev("ca--green", True, 100, now),
        _Rev("ca--blue", True, 0, now - timedelta(minutes=5)),
        _Rev("ca--old1", True, 0, now - timedelta(minutes=10)),
        _Rev("ca--old2", True, 0, now - timedelta(minutes=15)),
    ]
    images = {r.name: {f"myacr.azurecr.io/elb-api:{r.name}"} for r in revs}
    fake_rev = _FakeRevisions(revs, serving="ca--green", images=images)
    acr = _FakeAcr()

    result = revision_gc.collect_garbage_inline(
        keep_n=2, revisions_mod=fake_rev, acr_mod=acr
    )
    # serving (green) is protected; keep_n=2 retains green + blue.
    # old1, old2 are stale → deactivated.
    assert set(fake_rev.deactivated) == {"ca--old1", "ca--old2"}
    assert "ca--green" not in fake_rev.deactivated
    assert "ca--blue" not in fake_rev.deactivated
    assert set(result.deactivated) == {"ca--old1", "ca--old2"}


def test_keep_n_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """UPGRADE_REVISION_KEEP_N widens retention when keep_n is not passed."""
    monkeypatch.setenv("UPGRADE_REVISION_KEEP_N", "3")
    now = _now()
    revs = [
        _Rev("ca--green", True, 100, now),
        _Rev("ca--blue", True, 0, now - timedelta(minutes=5)),
        _Rev("ca--old1", True, 0, now - timedelta(minutes=10)),
        _Rev("ca--old2", True, 0, now - timedelta(minutes=15)),
    ]
    images = {r.name: {f"myacr.azurecr.io/elb-api:{r.name}"} for r in revs}
    fake_rev = _FakeRevisions(revs, serving="ca--green", images=images)

    # keep_n omitted → resolved from env (3) → green+blue+old1 retained,
    # only the oldest (old2) is deactivated.
    result = revision_gc.collect_garbage_inline(revisions_mod=fake_rev, acr_mod=_FakeAcr())
    assert set(result.deactivated) == {"ca--old2"}
    assert revision_gc.keep_n_revisions() == 3


def test_protected_blue_green_never_deactivated() -> None:
    now = _now()
    state.update_state(
        lambda s: (
            setattr(s, "green_revision", "ca--green"),
            setattr(s, "blue_revision", "ca--blue"),
        )[-1]
    )
    revs = [
        _Rev("ca--green", True, 100, now),
        _Rev("ca--blue", True, 0, now - timedelta(minutes=30)),  # old but protected
        _Rev("ca--newish", True, 0, now - timedelta(minutes=1)),
    ]
    images = {r.name: {f"myacr.azurecr.io/elb-api:{r.name}"} for r in revs}
    fake_rev = _FakeRevisions(revs, serving="ca--green", images=images)

    revision_gc.collect_garbage_inline(keep_n=1, revisions_mod=fake_rev, acr_mod=_FakeAcr())
    # green (serving+protected) and blue (protected) survive even though
    # keep_n=1 would otherwise only keep green + newish.
    assert "ca--blue" not in fake_rev.deactivated
    assert "ca--green" not in fake_rev.deactivated


def test_orphan_tags_deleted_but_retained_tags_kept() -> None:
    now = _now()
    revs = [
        _Rev("ca--green", True, 100, now),
        _Rev("ca--old", True, 0, now - timedelta(minutes=20)),
    ]
    images = {
        "ca--green": {"myacr.azurecr.io/elb-api:v0.3.0", "redis:7-alpine"},
        "ca--old": {"myacr.azurecr.io/elb-api:v0.2.0", "redis:7-alpine"},
    }
    fake_rev = _FakeRevisions(revs, serving="ca--green", images=images)
    acr = _FakeAcr()

    result = revision_gc.collect_garbage_inline(
        keep_n=1, revisions_mod=fake_rev, acr_mod=acr
    )
    # old's v0.2.0 tag is orphaned → deleted. redis is shared (retained) and
    # not a platform ACR ref → never touched. v0.3.0 is retained.
    assert result.deleted_tags == ["myacr.azurecr.io/elb-api:v0.2.0"]
    assert "redis:7-alpine" not in acr.delete_calls
    assert "myacr.azurecr.io/elb-api:v0.3.0" not in acr.delete_calls


def test_deactivate_failure_isolated() -> None:
    now = _now()
    revs = [
        _Rev("ca--green", True, 100, now),
        _Rev("ca--old1", True, 0, now - timedelta(minutes=10)),
        _Rev("ca--old2", True, 0, now - timedelta(minutes=15)),
    ]
    images = {r.name: set() for r in revs}
    fake_rev = _FakeRevisions(revs, serving="ca--green", images=images)
    fake_rev.deactivate_fail = {"ca--old1"}

    result = revision_gc.collect_garbage_inline(
        keep_n=1, revisions_mod=fake_rev, acr_mod=_FakeAcr()
    )
    assert result.deactivate_failed == ["ca--old1"]
    assert result.deactivated == ["ca--old2"]


def test_list_revisions_failure_skips_sweep() -> None:
    class _Boom(_FakeRevisions):
        def list_revisions(self):  # type: ignore[override]
            raise RuntimeError("arm down")

    fake_rev = _Boom([], serving="x", images={})
    result = revision_gc.collect_garbage_inline(revisions_mod=fake_rev, acr_mod=_FakeAcr())
    assert result.skipped_reason.startswith("list_revisions failed")
    assert result.deactivated == []
