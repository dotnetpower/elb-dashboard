"""Tests for the ACR image-retention helper (keep newest N, delete older).

Module summary: Drives `acr_retention` against a fake `ContainerRegistryClient`
that records `delete_manifest` calls, so no real registry is touched.

Responsibility: Verify the keep-newest-N selection, protected-tag/digest
  safety, env-driven keep override, graceful degradation on SDK errors, and
  the control-plane orchestration wrapper.
Edit boundaries: Update when `RepoPruneResult` or the prune contract changes.
Key entry points: Tests for `prune_repository`, `keep_count`,
  `_partition_protected`, `prune_control_plane_images`.
Risky contracts: Deletion must never touch the newest `keep` manifests or any
  protected ref, and must never raise.
Validation: `uv run pytest -q api/tests/test_upgrade_acr_retention.py`.
"""

from __future__ import annotations

import types
from datetime import UTC, datetime, timedelta

import pytest
from api.services.upgrade import acr_retention


class _FakeManifest:
    def __init__(
        self,
        digest: str,
        *,
        tags: list[str] | None = None,
        last_updated_on: datetime | None = None,
    ) -> None:
        self.digest = digest
        self.tags = tags or []
        self.last_updated_on = last_updated_on
        self.created_on = last_updated_on


class _FakeClient:
    def __init__(
        self,
        manifests: dict[str, list[_FakeManifest]],
        *,
        delete_raises: Exception | None = None,
        list_raises: Exception | None = None,
    ) -> None:
        self._manifests = manifests
        self._delete_raises = delete_raises
        self._list_raises = list_raises
        self.deleted: list[tuple[str, str]] = []
        self.closed = False

    def list_manifest_properties(self, repo: str, order_by: str | None = None):
        if self._list_raises is not None:
            raise self._list_raises
        # Return in arbitrary (oldest-first) order to prove the helper re-sorts.
        return list(self._manifests.get(repo, []))

    def delete_manifest(self, repo: str, digest: str) -> None:
        if self._delete_raises is not None:
            raise self._delete_raises
        self.deleted.append((repo, digest))
        self._manifests[repo] = [
            m for m in self._manifests.get(repo, []) if m.digest != digest
        ]

    def close(self) -> None:
        self.closed = True


def _gen(n: int, *, repo_tag_prefix: str = "v0.") -> list[_FakeManifest]:
    base = datetime(2026, 6, 1, tzinfo=UTC)
    out: list[_FakeManifest] = []
    for i in range(n):
        out.append(
            _FakeManifest(
                f"sha256:{i:064x}",
                tags=[f"{repo_tag_prefix}{i}.0"],
                last_updated_on=base + timedelta(hours=i),
            )
        )
    return out


def test_keep_count_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(acr_retention.KEEP_ENV, raising=False)
    assert acr_retention.keep_count() == acr_retention.DEFAULT_KEEP


def test_keep_count_env_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(acr_retention.KEEP_ENV, "5")
    assert acr_retention.keep_count() == 5
    # Explicit override wins and clamps to >= 1.
    assert acr_retention.keep_count(2) == 2
    assert acr_retention.keep_count(0) == 1


def test_keep_count_invalid_env_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(acr_retention.KEEP_ENV, "not-a-number")
    assert acr_retention.keep_count() == acr_retention.DEFAULT_KEEP


def test_prune_keeps_newest_n_and_deletes_older() -> None:
    manifests = {"elb-api": _gen(6)}  # indices 0..5, hour 0..5 (5 = newest)
    client = _FakeClient(manifests)
    result = acr_retention.prune_repository(
        "https://acr.azurecr.io", "elb-api", keep=3, client=client
    )
    # Newest 3 = indices 5,4,3 (digests for i=5,4,3) kept; 2,1,0 deleted.
    assert len(result.deleted) == 3
    deleted_digests = {d for d in result.deleted}
    assert f"sha256:{0:064x}" in deleted_digests
    assert f"sha256:{2:064x}" in deleted_digests
    assert f"sha256:{5:064x}" not in deleted_digests
    assert len(result.kept) == 3
    assert not result.errors


def test_prune_noop_when_at_or_below_keep() -> None:
    client = _FakeClient({"elb-api": _gen(2)})
    result = acr_retention.prune_repository(
        "https://acr.azurecr.io", "elb-api", keep=3, client=client
    )
    assert result.deleted == ()
    assert client.deleted == []


def test_prune_protects_tag_outside_window() -> None:
    manifests = {"elb-api": _gen(5)}  # i=0..4
    client = _FakeClient(manifests)
    # Protect the OLDEST tag (i=0) even though it is outside the newest-2 window.
    result = acr_retention.prune_repository(
        "https://acr.azurecr.io",
        "elb-api",
        keep=2,
        protected_tags=frozenset({"v0.0.0"}),
        client=client,
    )
    deleted = set(result.deleted)
    assert f"sha256:{0:064x}" not in deleted  # protected
    assert f"sha256:{0:064x}" in result.skipped_protected
    # i=1,2 deleted (outside window, not protected); i=3,4 kept (window).
    assert f"sha256:{1:064x}" in deleted
    assert f"sha256:{2:064x}" in deleted


def test_prune_protects_digest_outside_window() -> None:
    manifests = {"elb-api": _gen(5)}
    client = _FakeClient(manifests)
    protected_digest = f"sha256:{1:064x}"
    result = acr_retention.prune_repository(
        "https://acr.azurecr.io",
        "elb-api",
        keep=2,
        protected_digests=frozenset({protected_digest}),
        client=client,
    )
    assert protected_digest not in set(result.deleted)
    assert protected_digest in result.skipped_protected


def test_prune_forbidden_delete_is_best_effort() -> None:
    client = _FakeClient(
        {"elb-api": _gen(5)}, delete_raises=Exception("403 Forbidden")
    )
    result = acr_retention.prune_repository(
        "https://acr.azurecr.io", "elb-api", keep=2, client=client
    )
    assert result.deleted == ()
    assert any("forbidden" in e.lower() for e in result.errors)


def test_prune_list_not_found_is_noop() -> None:
    client = _FakeClient(
        {}, list_raises=Exception("RepositoryNotFound: 404")
    )
    result = acr_retention.prune_repository(
        "https://acr.azurecr.io", "elb-missing", keep=3, client=client
    )
    assert result.deleted == ()
    assert result.errors == ()


def test_prune_list_other_error_surfaces() -> None:
    client = _FakeClient({}, list_raises=Exception("500 boom"))
    result = acr_retention.prune_repository(
        "https://acr.azurecr.io", "elb-api", keep=3, client=client
    )
    assert result.deleted == ()
    assert any("list failed" in e for e in result.errors)


def test_partition_protected_splits_tags_and_digests() -> None:
    tags, digests = acr_retention._partition_protected(
        [
            "acr.azurecr.io/elb-api:v0.4.0",
            "acr.azurecr.io/elb-frontend@sha256:abc",
            "garbage",
            "",
        ]
    )
    assert tags == frozenset({"v0.4.0"})
    assert digests == frozenset({"sha256:abc"})


def test_prune_control_plane_no_acr_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(acr_retention.PLATFORM_ACR_NAME_ENV, raising=False)
    out = acr_retention.prune_control_plane_images()
    assert out["pruned"] is False


def test_prune_control_plane_runs_all_repos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(acr_retention.PLATFORM_ACR_NAME_ENV, "myacr")
    monkeypatch.setenv(acr_retention.KEEP_ENV, "1")
    manifests = {
        "elb-api": _gen(3),
        "elb-frontend": _gen(3),
        "elb-terminal": _gen(3),
    }
    client = _FakeClient(manifests)

    monkeypatch.setattr(
        acr_retention.acr_inventory, "_make_client", lambda endpoint: client
    )
    out = acr_retention.prune_control_plane_images(
        protected_image_refs=["myacr.azurecr.io/elb-api:v0.2.0"]
    )
    assert out["pruned"] is True
    assert out["keep"] == 1
    # Each repo had 3 manifests, keep=1 → 2 candidates each. elb-api protects
    # its v0.2.0 tag (i=2 newest is kept anyway; v0.2.0 = i=2 newest). The
    # protected tag is the newest so deletion count for elb-api is still 2.
    assert out["total_deleted"] >= 4
    assert {r["repo"] for r in out["repos"]} == {
        "elb-api",
        "elb-frontend",
        "elb-terminal",
    }


def test_reconciler_prune_hook_passes_protected_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_prune_acr_after_success` forwards the running + rollback image refs."""
    from api.tasks.upgrade import reconciler

    captured: dict[str, object] = {}

    def fake_prune(**kwargs: object) -> dict[str, int]:
        captured.update(kwargs)
        return {"total_deleted": 2}

    monkeypatch.setattr(
        "api.services.upgrade.acr_retention.prune_control_plane_images", fake_prune
    )
    row = types.SimpleNamespace(
        current_images_json='{"api": "acr.azurecr.io/elb-api:v0.4.0"}',
        rollback_target_json='{"api": "acr.azurecr.io/elb-api:v0.3.0"}',
    )
    reconciler._prune_acr_after_success(row)  # type: ignore[arg-type]
    refs = list(captured["protected_image_refs"])  # type: ignore[arg-type]
    assert "acr.azurecr.io/elb-api:v0.4.0" in refs
    assert "acr.azurecr.io/elb-api:v0.3.0" in refs


def test_reconciler_prune_hook_swallows_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prune failure must never propagate out of the success hook."""
    from api.tasks.upgrade import reconciler

    def boom(**_kwargs: object) -> dict[str, int]:
        raise RuntimeError("acr unreachable")

    monkeypatch.setattr(
        "api.services.upgrade.acr_retention.prune_control_plane_images", boom
    )
    row = types.SimpleNamespace(current_images_json="{}", rollback_target_json=None)
    # Must not raise.
    reconciler._prune_acr_after_success(row)  # type: ignore[arg-type]

