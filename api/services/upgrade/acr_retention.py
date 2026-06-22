"""ACR image retention helper for the self-upgrade flow.

Module summary: Enforces a "keep the newest N manifests per repository,
delete the rest" retention policy on the control-plane image repositories
(`elb-api`, `elb-frontend`, `elb-terminal`). Each in-app upgrade pushes one
new tag per repository, so without retention the registry accumulates a tag
per upgrade forever. This module bounds that growth from the data plane via
the `azure-containerregistry` SDK.

Responsibility: Pure retention computation + best-effort manifest deletion.
Edit boundaries: SDK construction is delegated to `acr_inventory`; this
  module owns only the keep-newest-N selection and the delete loop. No state
  machine writes, no Storage I/O.
Key entry points: `RepoPruneResult`, `prune_repository`,
  `prune_control_plane_images`, `CONTROL_PLANE_REPOS`, `DEFAULT_KEEP`.
Risky contracts: Deletion is irreversible. Two safeguards keep it safe: the
  newest `keep` manifests (by `last_updated_on`) are never deleted, and any
  manifest whose tag/digest is in the caller-supplied protected set (the
  currently-running images + the rollback target) is skipped even if it falls
  outside the newest window. All failures are swallowed (logged) so a missing
  `AcrDelete` permission or a transient registry outage can never fail the
  upgrade that triggered the prune. Reads the registry name from
  `PLATFORM_ACR_NAME` (same env `image_builder` builds into).
Validation: `uv run pytest -q api/tests/test_upgrade_acr_retention.py`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from api.services.upgrade import acr_inventory

LOGGER = logging.getLogger(__name__)

# Registry name env (mirrors api.services.upgrade.image_builder).
PLATFORM_ACR_NAME_ENV = "PLATFORM_ACR_NAME"
# Operator-tunable retention count. Default keeps the running image, the
# rollback target, and one extra generation of headroom.
KEEP_ENV = "UPGRADE_ACR_KEEP_IMAGES"
DEFAULT_KEEP = 3

# The image repositories that grow by one tag on every upgrade. The heavy
# `elb-terminal-base` layer is intentionally excluded: it changes rarely, is
# referenced as the build base of `elb-terminal`, and is keyed off the
# `latest` tag rather than a per-upgrade version tag.
CONTROL_PLANE_REPOS: tuple[str, ...] = ("elb-api", "elb-frontend", "elb-terminal")


@dataclass(frozen=True)
class RepoPruneResult:
    """Per-repository outcome of a retention prune (never raised, always returned)."""

    repo: str
    kept: tuple[str, ...] = ()  # digests retained (newest window + protected)
    deleted: tuple[str, ...] = ()  # digests actually deleted
    skipped_protected: tuple[str, ...] = ()  # digests outside window but protected
    errors: tuple[str, ...] = field(default_factory=tuple)  # human-readable reasons


def keep_count(override: int | None = None) -> int:
    """Resolve the retention count: explicit override > env > default.

    A value below 1 is clamped to 1 so a misconfiguration can never wipe a
    repository down to zero images.
    """
    if override is not None:
        return max(1, override)
    raw = os.environ.get(KEEP_ENV, "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            LOGGER.warning(
                "acr_retention: invalid %s=%r; using default %d",
                KEEP_ENV,
                raw,
                DEFAULT_KEEP,
            )
    return DEFAULT_KEEP


def _manifest_sort_key(props: Any) -> datetime:
    """Sort key: newest first by last_updated_on, falling back to created_on.

    Returns a timezone-aware sentinel (epoch) when neither timestamp is
    present so a manifest with missing metadata sorts oldest and never raises
    a mixed datetime/None comparison.
    """
    ts = getattr(props, "last_updated_on", None) or getattr(props, "created_on", None)
    if isinstance(ts, datetime):
        return ts
    return datetime.min.replace(tzinfo=UTC)


def prune_repository(
    endpoint: str,
    repo: str,
    *,
    keep: int,
    protected_tags: frozenset[str] = frozenset(),
    protected_digests: frozenset[str] = frozenset(),
    client: Any | None = None,
) -> RepoPruneResult:
    """Keep the newest ``keep`` manifests in ``repo``; delete the older ones.

    ``endpoint`` is the registry data-plane URL (``https://<acr>.azurecr.io``).
    A manifest is retained when it is in the newest ``keep`` window OR when any
    of its tags is in ``protected_tags`` / its digest is in
    ``protected_digests``. Never raises — every failure mode is captured in the
    returned ``errors`` tuple.
    """
    keep = max(1, keep)
    owns_client = client is None
    try:
        client = client if client is not None else acr_inventory._make_client(endpoint)
    except Exception as exc:  # pragma: no cover - construction failure is rare
        return RepoPruneResult(repo=repo, errors=(f"client init failed: {exc}",))

    try:
        try:
            manifests = list(client.list_manifest_properties(repo))
        except Exception as exc:
            msg = str(exc) or type(exc).__name__
            if "404" in msg or "NotFound" in msg or "RepositoryNotFound" in msg:
                return RepoPruneResult(repo=repo)  # nothing to prune yet
            return RepoPruneResult(repo=repo, errors=(f"list failed: {msg}",))

        # Newest first. We sort locally (rather than relying on a server-side
        # order_by, whose accepted string differs across SDK versions) using
        # the manifest's last_updated_on / created_on timestamp.
        manifests.sort(key=_manifest_sort_key, reverse=True)

        kept: list[str] = []
        deleted: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []

        for index, props in enumerate(manifests):
            digest = getattr(props, "digest", "") or ""
            tags = frozenset(getattr(props, "tags", None) or ())
            is_protected = digest in protected_digests or bool(tags & protected_tags)
            if index < keep:
                kept.append(digest)
                continue
            if is_protected:
                kept.append(digest)
                skipped.append(digest)
                continue
            if not digest:
                errors.append("manifest without digest skipped")
                continue
            try:
                client.delete_manifest(repo, digest)
                deleted.append(digest)
            except Exception as exc:
                msg = str(exc) or type(exc).__name__
                if "403" in msg or "Forbidden" in msg or "AuthorizationFailed" in msg:
                    errors.append(f"forbidden (MI needs AcrDelete): {digest}")
                elif "404" in msg or "NotFound" in msg:
                    deleted.append(digest)  # already gone — idempotent success
                else:
                    errors.append(f"delete {digest} failed: {msg}")

        return RepoPruneResult(
            repo=repo,
            kept=tuple(kept),
            deleted=tuple(deleted),
            skipped_protected=tuple(skipped),
            errors=tuple(errors),
        )
    finally:
        if owns_client:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:  # pragma: no cover - close best-effort
                    LOGGER.debug("acr client close failed: %s", exc)


def _partition_protected(
    image_refs: Iterable[str],
) -> tuple[frozenset[str], frozenset[str]]:
    """Split image refs into ``(protected_tags, protected_digests)``.

    A tag ref (``acr/repo:tag``) protects that tag across every repository —
    correct here because all three control-plane images share the same
    ``v<version>`` tag. A digest ref (``acr/repo@sha256:…``) protects only its
    own content-addressed manifest. Garbage refs are ignored.
    """
    tags: set[str] = set()
    digests: set[str] = set()
    for ref in image_refs:
        if not ref:
            continue
        try:
            _endpoint, _repo, reference = acr_inventory.parse_image_ref(str(ref))
        except ValueError:
            continue
        if reference.startswith("@"):
            digests.add(reference[1:])  # strip leading '@' → 'sha256:<hex>'
        else:
            tags.add(reference)
    return frozenset(tags), frozenset(digests)


def prune_control_plane_images(
    *,
    acr_name: str | None = None,
    keep: int | None = None,
    protected_image_refs: Iterable[str] = (),
    repos: Iterable[str] = CONTROL_PLANE_REPOS,
) -> dict[str, Any]:
    """Prune every control-plane repository down to the newest ``keep`` tags.

    Best-effort: a missing registry name, a registry outage, or a per-repo
    failure is logged and reflected in the result, never raised. Returns a
    summary dict suitable for logging / audit.
    """
    name = (acr_name or os.environ.get(PLATFORM_ACR_NAME_ENV, "")).strip()
    if not name:
        LOGGER.info("acr_retention: %s unset; skipping prune", PLATFORM_ACR_NAME_ENV)
        return {"pruned": False, "reason": "acr name unset", "repos": []}

    endpoint = f"https://{name.lower()}.azurecr.io"
    effective_keep = keep_count(keep)
    protected_tags, protected_digests = _partition_protected(protected_image_refs)

    results: list[RepoPruneResult] = []
    total_deleted = 0
    for repo in repos:
        try:
            result = prune_repository(
                endpoint,
                repo,
                keep=effective_keep,
                protected_tags=protected_tags,
                protected_digests=protected_digests,
            )
        except Exception as exc:  # pragma: no cover - prune_repository never raises
            LOGGER.warning("acr_retention: prune %s raised: %s", repo, exc)
            result = RepoPruneResult(repo=repo, errors=(str(exc),))
        results.append(result)
        total_deleted += len(result.deleted)
        if result.deleted or result.errors:
            LOGGER.info(
                "acr_retention: repo=%s kept=%d deleted=%d protected=%d errors=%d",
                repo,
                len(result.kept),
                len(result.deleted),
                len(result.skipped_protected),
                len(result.errors),
            )

    return {
        "pruned": True,
        "keep": effective_keep,
        "registry": name,
        "total_deleted": total_deleted,
        "repos": [
            {
                "repo": r.repo,
                "kept": list(r.kept),
                "deleted": list(r.deleted),
                "skipped_protected": list(r.skipped_protected),
                "errors": list(r.errors),
            }
            for r in results
        ],
    }
