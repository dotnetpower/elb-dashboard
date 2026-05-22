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
