"""Tests for the ACR data-plane inventory helper.

Module summary: Stubs `ContainerRegistryClient` via the factory injection
seam so no real ACR is touched.

Responsibility: Verify image-ref parsing, batch lookup, and error
  surfacing.
Edit boundaries: Update when the ImageInfo shape changes.
Key entry points: Tests for parse, lookup happy path, missing tag,
  malformed ref tolerance.
Risky contracts: `lookup_images` never raises — bad refs/SDK errors
  surface as `exists=False` + `error`.
Validation: `uv run pytest -q api/tests/test_upgrade_acr_inventory.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from api.services.upgrade import acr_inventory


class _FakeProps:
    def __init__(self, created: datetime | None) -> None:
        self.created_on = created


class _FakeClient:
    def __init__(self, *, exists_tags: set[tuple[str, str]], created: datetime) -> None:
        self._exists = exists_tags
        self._created = created
        self.closed = False

    def get_tag_properties(self, repo: str, tag: str) -> _FakeProps:
        if (repo, tag) not in self._exists:
            raise Exception("TagNotFound: not present")
        return _FakeProps(self._created)

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_factory() -> None:
    acr_inventory.set_client_factory_for_tests(None)
    yield
    acr_inventory.set_client_factory_for_tests(None)


def test_parse_image_ref_happy() -> None:
    endpoint, repo, tag = acr_inventory.parse_image_ref(
        "myacr.azurecr.io/ncbi/elb:v1.4.0"
    )
    assert endpoint == "https://myacr.azurecr.io"
    assert repo == "ncbi/elb"
    assert tag == "v1.4.0"


def test_parse_image_ref_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        acr_inventory.parse_image_ref("not-a-ref")
    with pytest.raises(ValueError):
        acr_inventory.parse_image_ref("missing-tag/elb")


def test_lookup_returns_per_image_status() -> None:
    created = datetime(2026, 5, 22, tzinfo=UTC)
    fake = _FakeClient(
        exists_tags={("elb-api", "v0.2.0"), ("elb-frontend", "v0.2.0")},
        created=created,
    )
    acr_inventory.set_client_factory_for_tests(lambda _ep: fake)

    out = acr_inventory.lookup_images(
        [
            "myacr.azurecr.io/elb-api:v0.2.0",
            "myacr.azurecr.io/elb-frontend:v0.2.0",
            "myacr.azurecr.io/elb-terminal:v0.2.0",
        ]
    )
    assert [r.exists for r in out] == [True, True, False]
    assert out[0].created_on == created
    assert "TagNotFound" in out[2].error or "not present" in out[2].error
    assert fake.closed is True  # client closed at end of batch


def test_lookup_tolerates_garbage_refs() -> None:
    fake = _FakeClient(exists_tags=set(), created=datetime(2026, 5, 22, tzinfo=UTC))
    acr_inventory.set_client_factory_for_tests(lambda _ep: fake)

    out = acr_inventory.lookup_images(["garbage", "myacr.azurecr.io/elb-api:v0.2.0"])
    assert out[0].exists is False
    assert "unsupported" in out[0].error.lower()
    assert out[1].exists is False  # tag not in fake's set


def test_image_exists_shortcut() -> None:
    fake = _FakeClient(
        exists_tags={("elb-api", "v0.2.0")},
        created=datetime(2026, 5, 22, tzinfo=UTC),
    )
    acr_inventory.set_client_factory_for_tests(lambda _ep: fake)
    assert acr_inventory.image_exists("myacr.azurecr.io/elb-api:v0.2.0") is True
    assert acr_inventory.image_exists("myacr.azurecr.io/elb-api:v9.9.9") is False


def test_delete_tag_best_effort_success() -> None:
    """Happy path: the client exposes `delete_tag` and returns cleanly."""

    class _DeletingClient:
        def __init__(self) -> None:
            self.deleted: list[tuple[str, str]] = []

        def delete_tag(self, repo: str, tag: str) -> None:
            self.deleted.append((repo, tag))

        def close(self) -> None:
            pass

    fake = _DeletingClient()
    acr_inventory.set_client_factory_for_tests(lambda _ep: fake)
    ok, reason = acr_inventory.delete_tag_best_effort(
        "myacr.azurecr.io/elb-api:v0.3.0"
    )
    assert ok is True
    assert reason == "deleted"
    assert fake.deleted == [("elb-api", "v0.3.0")]


def test_delete_tag_best_effort_forbidden_does_not_raise() -> None:
    """When the MI lacks `acrDelete`, the helper returns False with a
    precise reason instead of raising \u2014 the caller can fall back to
    recording the orphan in audit.
    """

    class _ForbiddenClient:
        def delete_tag(self, _repo: str, _tag: str) -> None:
            raise Exception("(Forbidden) 403 AuthorizationFailed")

        def close(self) -> None:
            pass

    acr_inventory.set_client_factory_for_tests(lambda _ep: _ForbiddenClient())
    ok, reason = acr_inventory.delete_tag_best_effort(
        "myacr.azurecr.io/elb-api:v0.3.0"
    )
    assert ok is False
    assert "forbidden" in reason.lower()


def test_delete_tag_best_effort_already_absent_is_idempotent_success() -> None:
    """`TagNotFound` means the tag is already gone \u2014 idempotent success."""

    class _MissingClient:
        def delete_tag(self, _repo: str, _tag: str) -> None:
            raise Exception("TagNotFound: 404")

        def close(self) -> None:
            pass

    acr_inventory.set_client_factory_for_tests(lambda _ep: _MissingClient())
    ok, reason = acr_inventory.delete_tag_best_effort(
        "myacr.azurecr.io/elb-api:v0.3.0"
    )
    assert ok is True
    assert reason == "already absent"


def test_delete_tag_best_effort_malformed_ref_is_safe() -> None:
    ok, reason = acr_inventory.delete_tag_best_effort("garbage")
    assert ok is False
    assert "unsupported" in reason.lower()


# ---------------------------------------------------------------------------
# Digest-pinned references (root cause of the spurious "Rollback unsafe …
# TagNotFound" gate when the live Container App was deployed by digest).
# ---------------------------------------------------------------------------


def test_parse_image_ref_digest() -> None:
    """A digest pin parses into the repo + an '@sha256:…' reference, NOT a
    'repo@sha256' / '<hex>' tag split."""
    endpoint, repo, ref = acr_inventory.parse_image_ref(
        "myacr.azurecr.io/elb-api@sha256:616703994a45a801db7bde72635c981fa6f9c45c"
    )
    assert endpoint == "https://myacr.azurecr.io"
    assert repo == "elb-api"
    assert ref == "@sha256:616703994a45a801db7bde72635c981fa6f9c45c"


def test_parse_image_ref_digest_with_nested_repo() -> None:
    endpoint, repo, ref = acr_inventory.parse_image_ref(
        "myacr.azurecr.io/ncbi/elb@sha256:deadbeef"
    )
    assert endpoint == "https://myacr.azurecr.io"
    assert repo == "ncbi/elb"
    assert ref == "@sha256:deadbeef"


class _DigestClient:
    """Fake ACR client that resolves digests via get_manifest_properties and
    tags via get_tag_properties, keyed on the exact reference string."""

    def __init__(self, *, manifests: set[tuple[str, str]], created: datetime) -> None:
        self._manifests = manifests
        self._created = created
        self.closed = False
        self.manifest_calls: list[tuple[str, str]] = []

    def get_manifest_properties(self, repo: str, digest: str) -> _FakeProps:
        self.manifest_calls.append((repo, digest))
        if (repo, digest) not in self._manifests:
            raise Exception("ManifestUnknown: not present")
        return _FakeProps(self._created)

    def get_tag_properties(self, repo: str, tag: str) -> _FakeProps:
        raise AssertionError(
            f"digest probe must not hit the tag API (repo={repo}, tag={tag})"
        )

    def close(self) -> None:
        self.closed = True


def test_lookup_digest_pin_resolves_via_manifest_api() -> None:
    """Regression: a digest-pinned rollback target must resolve via the
    manifest API and report exists=True — previously it was split as a tag
    and always returned TagNotFound, falsely tripping 'Rollback unsafe'."""
    created = datetime(2026, 6, 6, tzinfo=UTC)
    digest = "sha256:616703994a45a801db7bde72635c981fa6f9c45c"
    fake = _DigestClient(manifests={("elb-api", digest)}, created=created)
    acr_inventory.set_client_factory_for_tests(lambda _ep: fake)

    out = acr_inventory.lookup_images([f"myacr.azurecr.io/elb-api@{digest}"])

    assert out[0].exists is True
    assert out[0].created_on == created
    assert fake.manifest_calls == [("elb-api", digest)]


def test_lookup_digest_pin_missing_reports_manifest_not_found() -> None:
    fake = _DigestClient(manifests=set(), created=datetime(2026, 6, 6, tzinfo=UTC))
    acr_inventory.set_client_factory_for_tests(lambda _ep: fake)

    out = acr_inventory.lookup_images(
        ["myacr.azurecr.io/elb-api@sha256:deadbeefdeadbeef"]
    )
    assert out[0].exists is False
    assert "ManifestUnknown" in out[0].error or "ManifestNotFound" in out[0].error


def test_image_exists_digest_shortcut() -> None:
    digest = "sha256:abc123"
    fake = _DigestClient(
        manifests={("elb-api", digest)}, created=datetime(2026, 6, 6, tzinfo=UTC)
    )
    acr_inventory.set_client_factory_for_tests(lambda _ep: fake)
    assert acr_inventory.image_exists(f"myacr.azurecr.io/elb-api@{digest}") is True
    assert (
        acr_inventory.image_exists("myacr.azurecr.io/elb-api@sha256:missing") is False
    )
