"""Discover release-version candidates from a configured git remote.

Module summary: Calls the git smart HTTP discovery endpoint to enumerate
``refs/tags/vX.Y.Z`` on a remote without requiring a `git` binary in the api
sidecar. Used by the in-app self-upgrade flow's read-only path to populate
the SPA's "upgrade available" indicator.

Responsibility: Read-only git remote ref discovery for the upgrade flow.
Edit boundaries: HTTP I/O against the git smart-protocol endpoint lives here;
  no Azure SDK imports, no Storage writes, no Celery wiring.
Key entry points: `RemoteTag`, `configured_remote`, `mask_remote_url`,
  `fetch_release_tags`, `filter_candidates`, `RemoteTagsError`.
Risky contracts: The remote URL MUST originate from the `UPGRADE_GIT_REMOTE`
  env — never from a request body / query param — because this function
  issues anonymous HTTPS GETs against the operator-supplied URL.
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
HTTP_TIMEOUT_SECONDS = 10.0
MAX_TAGS = 20
MAX_RESPONSE_BYTES = 4 * 1024 * 1024  # 4 MiB — generous for any real refs list.

_RELEASE_TAG = re.compile(r"^refs/tags/v(\d+\.\d+\.\d+)$")
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
    """Return the operator-configured git remote URL, or ``None`` when unset.

    Reads ``UPGRADE_GIT_REMOTE`` from the environment. No default is supplied:
    the upgrade flow stays inert until an operator opts in by setting the
    env variable on the Container App. This avoids accidentally fetching
    from an upstream the operator does not control.

    SECURITY: This is the ONLY supported input channel for the remote URL.
    Routes and tasks must never accept a remote URL from request bodies,
    query parameters, or other caller-controlled inputs — doing so would
    open a server-side request forgery (SSRF) hole through this module.
    """
    value = os.environ.get(UPGRADE_GIT_REMOTE_ENV, "").strip()
    return value or None


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
    return url


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

    refs = _parse_pkt_lines(content)

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
    """Return tags strictly greater than ``running_version`` (semver compare)."""
    try:
        cur = Version(running_version)
    except InvalidVersion:
        return list(tags)
    return [t for t in tags if Version(t.name) > cur]
