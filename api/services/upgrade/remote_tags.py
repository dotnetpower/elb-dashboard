"""Discover release-version candidates from a configured git remote.

Module summary: Calls the git smart HTTP discovery endpoint to enumerate
``refs/tags/vX.Y.Z`` on a remote without requiring a `git` binary in the api
sidecar. Used by the in-app self-upgrade flow's read-only path to populate
the SPA's "upgrade available" indicator.

Responsibility: Read-only git remote ref discovery for the upgrade flow.
Edit boundaries: HTTP I/O against the git smart-protocol endpoint lives here;
  no Azure SDK imports, no Storage writes, no Celery wiring.
Key entry points: `RemoteTag`, `configured_remote`, `mask_remote_url`,
  `fetch_release_tags`, `fetch_branch_head`, `filter_candidates`,
  `RemoteTagsError`, `DEFAULT_GIT_REMOTE`, `DEFAULT_TRACK_BRANCH`.
Risky contracts: The remote URL MUST originate from the `UPGRADE_GIT_REMOTE`
  env or the in-code `DEFAULT_GIT_REMOTE` — never from a request body / query
  param — because this function issues anonymous HTTPS GETs against that URL.
  Additional in-code guards: regex shape check, refusal of the Azure
  Instance Metadata Service IP (`169.254.169.254`), response-body cap at
  `MAX_RESPONSE_BYTES`, and URL credential masking before logging.
  Private VNet remotes (RFC1918 IPs) are intentionally NOT blocked —
  an operator running an internal git server is a legitimate case.
Validation: `uv run pytest -q api/tests/test_upgrade_remote_tags.py`.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from dataclasses import asdict, dataclass
from urllib.parse import urlparse, urlunparse

import httpx
from packaging.version import InvalidVersion, Version

LOGGER = logging.getLogger(__name__)

UPGRADE_GIT_REMOTE_ENV = "UPGRADE_GIT_REMOTE"
UPGRADE_GIT_REMOTE_ALLOWLIST_ENV = "UPGRADE_GIT_REMOTE_ALLOWLIST"
HTTP_TIMEOUT_SECONDS = 10.0
MAX_TAGS = 20
MAX_RESPONSE_BYTES = 4 * 1024 * 1024  # 4 MiB — generous for any real refs list.

# Default upstream so the update-check flow works with zero configuration.
# Operators can still override or disable via `UPGRADE_GIT_REMOTE`. This is
# the project's own public HTTPS git remote — trusted, anonymous, read-only.
# It is read through the module attribute (not captured) so tests can blank
# it to exercise the legacy "no remote configured" branch.
DEFAULT_GIT_REMOTE = "https://github.com/dotnetpower/elb-dashboard.git"
# Branch the "new commits" channel tracks when `track_commits` is on.
DEFAULT_TRACK_BRANCH = "main"

_RELEASE_TAG = re.compile(r"^refs/tags/v(\d+\.\d+\.\d+)$")
_HEAD_REF_PREFIX = "refs/heads/"
_REMOTE_URL_RE = re.compile(r"^https?://[\w.\-:@/]+\.git$")
# Hosts that must never be reachable through this module. Limited to
# cloud-provider metadata services and loopback — the primary SSRF
# defense is that the URL only comes from env, not from request bodies.
_BANNED_LITERAL_HOSTS: frozenset[str] = frozenset(
    {"localhost", "metadata.google.internal", "metadata.azure.com"}
)
_BANNED_IPS: frozenset[str] = frozenset(
    {"169.254.169.254", "fd00:ec2::254"}  # AWS / Azure / GCP IMDS variants
)


class RemoteTagsError(RuntimeError):
    """Raised when the remote cannot be reached or returns an unexpected payload."""


@dataclass(frozen=True)
class RemoteTag:
    """A single release tag discovered on the configured remote."""

    name: str  # Semver string without the leading "v" — e.g. "0.3.0"
    raw_ref: str  # "refs/tags/v0.3.0"
    commit_sha: str  # 40-char hex

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def configured_remote() -> str | None:
    """Return the effective git remote URL, or ``None`` when none is set.

    Resolution order:
      1. ``UPGRADE_GIT_REMOTE`` env (operator override) when non-empty.
      2. :data:`DEFAULT_GIT_REMOTE` — the project's own public remote, so
         the update-check flow works out of the box with zero configuration.

    Returns ``None`` only when both are empty (i.e. an operator has blanked
    the default in code). The historical "inert until opted in" behaviour is
    therefore reachable by setting ``DEFAULT_GIT_REMOTE = ""``.

    SECURITY: env and the in-code default are the ONLY supported input
    channels for the remote URL. Routes and tasks must never accept a remote
    URL from request bodies, query parameters, or other caller-controlled
    inputs — doing so would open a server-side request forgery (SSRF) hole
    through this module.
    """
    value = os.environ.get(UPGRADE_GIT_REMOTE_ENV, "").strip()
    if value:
        return value
    default = (DEFAULT_GIT_REMOTE or "").strip()
    return default or None


def mask_remote_url(url: str) -> str:
    """Strip any embedded `user:password@` from ``url`` before logging.

    `urlparse` reliably splits the userinfo component so credentials never
    leak into log messages, audit blobs, or error strings — even if a
    future PR introduces PAT-prefixed remotes (`https://x-access-token:...@`).
    """
    try:
        parts = urlparse(url)
    except ValueError:
        return "<unparseable url>"
    if not parts.hostname:
        return url
    netloc = parts.hostname
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunparse(parts._replace(netloc=netloc))


def _validate_url(url: str) -> str:
    if not _REMOTE_URL_RE.match(url):
        raise RemoteTagsError(f"unsupported remote URL shape: {mask_remote_url(url)!r}")
    parts = urlparse(url)
    hostname = (parts.hostname or "").lower()
    if not hostname:
        raise RemoteTagsError("remote URL is missing a hostname")
    if hostname in _BANNED_LITERAL_HOSTS:
        raise RemoteTagsError(f"remote URL hostname {hostname!r} is not permitted")
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None
    if ip is not None and (str(ip) in _BANNED_IPS or ip.is_loopback):
        raise RemoteTagsError(
            f"remote URL hostname {hostname!r} resolves to a banned address"
        )
    # Optional positive allowlist. When `UPGRADE_GIT_REMOTE_ALLOWLIST`
    # is set (comma-separated hostnames, case-insensitive), only refs
    # under those hosts are accepted. The default — unset — preserves
    # backwards-compatible permissive behaviour. Allowlist matches the
    # full hostname (no wildcards) so a deliberate strict policy cannot
    # be widened by a typo.
    allowlist_raw = os.environ.get(UPGRADE_GIT_REMOTE_ALLOWLIST_ENV, "").strip()
    if allowlist_raw:
        allowed = {h.strip().lower() for h in allowlist_raw.split(",") if h.strip()}
        if hostname not in allowed:
            raise RemoteTagsError(
                f"remote URL hostname {hostname!r} is not in "
                f"{UPGRADE_GIT_REMOTE_ALLOWLIST_ENV}"
            )
    return url


def _advertise_refs(
    remote_url: str,
    *,
    timeout_seconds: float,
    max_response_bytes: int,
    http_client_factory: type[httpx.Client],
) -> list[tuple[str, str]]:
    """Fetch + parse the smart-protocol advertisement into (sha, ref) pairs.

    Shared by :func:`fetch_release_tags` and :func:`fetch_refs` so the
    network call (and its SSRF / size guards) lives in exactly one place.
    """
    url = _validate_url(remote_url)
    endpoint = f"{url.rstrip('/')}/info/refs"
    params = {"service": "git-upload-pack"}
    headers = {
        "Accept": "application/x-git-upload-pack-advertisement",
        "User-Agent": "elb-dashboard-upgrade/1.0",
    }
    masked = mask_remote_url(url)
    try:
        # SECURITY: follow_redirects MUST stay False. The URL is allow-listed
        # by `_validate_url`, but redirects are not — a malicious upstream
        # could redirect us at the Azure IMDS (169.254.169.254) and bypass
        # the SSRF guard. Git's smart-protocol `/info/refs` does not rely
        # on redirects for any legitimate hosting (GitHub / GitLab / gitea
        # all serve the endpoint directly).
        with http_client_factory(timeout=timeout_seconds, follow_redirects=False) as client:
            resp = client.get(endpoint, params=params, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise RemoteTagsError(f"git ls-remote HTTP error for {masked}: {exc}") from exc

    content = resp.content
    if len(content) > max_response_bytes:
        raise RemoteTagsError(
            f"git ls-remote response from {masked} exceeded {max_response_bytes} bytes"
        )
    return _parse_pkt_lines(content)


def _tags_from_refs(refs: list[tuple[str, str]], *, max_tags: int) -> list[RemoteTag]:
    """Filter advertisement (sha, ref) pairs to sorted semver release tags."""
    tags: list[RemoteTag] = []
    seen: set[str] = set()
    for sha, ref in refs:
        match = _RELEASE_TAG.match(ref)
        if not match:
            continue
        semver_str = match.group(1)
        if semver_str in seen:
            continue
        try:
            Version(semver_str)
        except InvalidVersion:
            continue
        seen.add(semver_str)
        tags.append(RemoteTag(name=semver_str, raw_ref=ref, commit_sha=sha.lower()))

    tags.sort(key=lambda t: Version(t.name), reverse=True)
    return tags[:max_tags]


def _branch_head_from_refs(refs: list[tuple[str, str]], *, branch: str) -> str:
    """Return the lowercased commit sha advertised for ``refs/heads/<branch>``.

    Returns ``""`` when the branch is not advertised (so the caller can treat
    a missing branch as "no commit channel target" rather than an error).
    """
    target = f"{_HEAD_REF_PREFIX}{branch}"
    for sha, ref in refs:
        if ref == target:
            return sha.lower()
    return ""


def fetch_release_tags(
    remote_url: str,
    *,
    timeout_seconds: float = HTTP_TIMEOUT_SECONDS,
    max_tags: int = MAX_TAGS,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
    http_client_factory: type[httpx.Client] = httpx.Client,
) -> list[RemoteTag]:
    """Return semver tags advertised by the remote, newest first.

    Speaks the git smart HTTP discovery endpoint
    (``GET <remote>/info/refs?service=git-upload-pack``) and parses the
    pkt-line stream. Only ``refs/tags/vX.Y.Z`` entries are returned; other
    refs (heads, peeled tag entries with ``^{}``, GitHub PR refs) are
    skipped. Anonymous request only — private remotes are out of scope for
    this PR.
    """
    refs = _advertise_refs(
        remote_url,
        timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes,
        http_client_factory=http_client_factory,
    )
    return _tags_from_refs(refs, max_tags=max_tags)


def fetch_branch_head(
    remote_url: str,
    *,
    branch: str = DEFAULT_TRACK_BRANCH,
    timeout_seconds: float = HTTP_TIMEOUT_SECONDS,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
    http_client_factory: type[httpx.Client] = httpx.Client,
) -> str:
    """Return the tracking branch's HEAD commit sha, or ``""`` when absent.

    Used by the "new commits" channel as a best-effort augmentation of the
    release-tag check: the caller fetches release tags first (the primary,
    well-tested path) and then calls this to learn whether the tracking
    branch has moved past the running commit. Returns ``""`` when the branch
    is not advertised so a missing branch degrades to "no commit target".
    """
    refs = _advertise_refs(
        remote_url,
        timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes,
        http_client_factory=http_client_factory,
    )
    return _branch_head_from_refs(refs, branch=branch)


def _parse_pkt_lines(raw: bytes) -> list[tuple[str, str]]:
    """Yield (sha, ref) pairs from a smart-protocol advertisement."""
    out: list[tuple[str, str]] = []
    i = 0
    n = len(raw)
    while i + 4 <= n:
        length_hex = raw[i : i + 4]
        try:
            length = int(length_hex, 16)
        except ValueError:
            break
        if length == 0:
            # Flush packet — boundary between sections.
            i += 4
            continue
        if length < 4 or i + length > n:
            break
        payload = raw[i + 4 : i + length]
        i += length
        line = payload.rstrip(b"\n").decode("utf-8", errors="replace")
        # The first payload line is "# service=git-upload-pack" — skip.
        if line.startswith("# "):
            continue
        # The very first ref line carries NUL-separated capabilities:
        # "<sha> <ref>\0capability list". Strip the suffix.
        if "\x00" in line:
            line = line.split("\x00", 1)[0]
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        sha, ref = parts
        # Peeled tag refs look like "refs/tags/X^{}" and carry the
        # underlying commit sha — skip them so the tag object sha wins.
        if ref.endswith("^{}"):
            continue
        if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha.lower()):
            continue
        out.append((sha, ref))
    return out


def filter_candidates(
    tags: list[RemoteTag],
    *,
    running_version: str,
) -> list[RemoteTag]:
    """Return tags strictly greater than ``running_version`` (semver compare).

    ``running_version`` may be a commit target (``<base>-commit.<sha>``) when
    the running image was built from the commit channel; it is reduced to its
    bare-semver base before the compare so ``packaging.version.Version`` never
    sees the non-PEP-440 commit suffix.
    """
    from api.services.upgrade.version_target import base_release

    try:
        cur = Version(base_release(running_version))
    except InvalidVersion:
        return list(tags)
    return [t for t in tags if Version(t.name) > cur]
