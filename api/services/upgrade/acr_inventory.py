"""ACR data-plane inventory helpers for the upgrade flow.

Module summary: Thin wrapper around `azure-containerregistry` that the
rollback path uses to verify the snapshotted image tags still resolve
in the platform ACR before the operator commits to a PATCH that ACA
cannot satisfy.

Responsibility: Read-only ACR manifest existence + creation timestamp.
Edit boundaries: SDK construction lives here; tasks/routes consume the
  data classes.
Key entry points: `ImageInfo`, `lookup_images`, `image_exists`,
  `parse_image_ref`, `set_client_factory_for_tests`.
Risky contracts: Uses anonymous Managed Identity creds via
  `get_credential()`. Requires `acrPull` on the registry (already
  granted to the user-assigned MI).
Validation: `uv run pytest -q api/tests/test_upgrade_acr_inventory.py`.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from api.services import get_credential

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImageInfo:
    """Per-image existence + age summary used by the rollback pre-flight."""

    image_ref: str  # `<acr>.azurecr.io/<repo>:<tag>`
    exists: bool
    created_on: datetime | None = None
    error: str = ""


def parse_image_ref(image_ref: str) -> tuple[str, str, str]:
    """Split `acr.azurecr.io/repo:tag` into ``(acr_endpoint, repo, tag)``.

    Raises ValueError on garbage input so the rollback path fails fast.
    """
    if not image_ref or "/" not in image_ref or ":" not in image_ref.rsplit("/", 1)[-1]:
        raise ValueError(f"unsupported image reference: {image_ref!r}")
    host, rest = image_ref.split("/", 1)
    repo, tag = rest.rsplit(":", 1)
    if not host or not repo or not tag:
        raise ValueError(f"image reference missing host/repo/tag: {image_ref!r}")
    return f"https://{host}", repo, tag


_FACTORY_LOCK = threading.Lock()
_CLIENT_FACTORY: Callable[[str], Any] | None = None


def set_client_factory_for_tests(factory: Callable[[str], Any] | None) -> None:
    """Inject a fake ContainerRegistryClient factory for unit tests."""
    global _CLIENT_FACTORY
    with _FACTORY_LOCK:
        _CLIENT_FACTORY = factory


def _make_client(endpoint: str) -> Any:
    if _CLIENT_FACTORY is not None:
        return _CLIENT_FACTORY(endpoint)
    from azure.containerregistry import ContainerRegistryClient

    return ContainerRegistryClient(endpoint=endpoint, credential=get_credential())


def lookup_images(image_refs: list[str]) -> list[ImageInfo]:
    """Return per-image existence info, in the order the refs were supplied.

    Never raises — each entry's `exists` / `error` reflects its own
    lookup result so a partial outage still produces a usable answer.
    """
    out: list[ImageInfo] = []
    # Group by ACR endpoint so we reuse the client for sibling images.
    grouped: dict[str, list[tuple[int, str, str]]] = {}
    parsed_errors: dict[int, str] = {}
    for index, ref in enumerate(image_refs):
        try:
            endpoint, repo, tag = parse_image_ref(ref)
        except ValueError as exc:
            parsed_errors[index] = str(exc)
            continue
        grouped.setdefault(endpoint, []).append((index, repo, tag))

    results: dict[int, ImageInfo] = {}
    for endpoint, entries in grouped.items():
        client = _make_client(endpoint)
        try:
            for index, repo, tag in entries:
                results[index] = _probe(client, image_refs[index], repo, tag)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    LOGGER.debug("acr client close failed: %s", exc)

    for index, ref in enumerate(image_refs):
        if index in parsed_errors:
            out.append(
                ImageInfo(image_ref=ref, exists=False, error=parsed_errors[index])
            )
        elif index in results:
            out.append(results[index])
        else:
            out.append(ImageInfo(image_ref=ref, exists=False, error="not probed"))
    return out


def _probe(client: Any, image_ref: str, repo: str, tag: str) -> ImageInfo:
    # Lazy import — keeps `azure-core` out of import-time cost for callers
    # that never reach the ACR path.
    try:
        from azure.core.exceptions import ResourceNotFoundError
    except ImportError:  # pragma: no cover - azure-core ships with the data plane
        ResourceNotFoundError = None  # type: ignore[assignment]
    try:
        props = client.get_tag_properties(repo, tag)
    except Exception as exc:  # noqa: BLE001 - SDK raises a variety
        # Distinguish "not found" (= retention purge) from "registry
        # offline" so the upstream rollback gate stays precise.
        if ResourceNotFoundError is not None and isinstance(exc, ResourceNotFoundError):
            return ImageInfo(image_ref=image_ref, exists=False, error="TagNotFound")
        msg = str(exc)
        if "TagNotFound" in msg or "ManifestUnknown" in msg or "404" in msg:
            return ImageInfo(image_ref=image_ref, exists=False, error=msg)
        return ImageInfo(image_ref=image_ref, exists=False, error=f"acr lookup error: {msg}")
    created = getattr(props, "created_on", None)
    return ImageInfo(image_ref=image_ref, exists=True, created_on=created)


def image_exists(image_ref: str) -> bool:
    """Shortcut: True if and only if the image manifest resolves in ACR."""
    info = lookup_images([image_ref])
    return bool(info and info[0].exists)
