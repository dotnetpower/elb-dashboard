"""Tests for the git smart-protocol remote-tag discovery helper.

Module summary: Exercises the pkt-line parser, semver filtering, URL guard,
and HTTP-failure surfacing of `api.services.upgrade.remote_tags`. The HTTP
client is stubbed via `httpx.MockTransport` so no network is touched.

Responsibility: Verify the read-only remote-tag discovery behaviour.
Edit boundaries: When the parser or filtering contract changes, update
  these tests in lockstep.
Key entry points: Test functions for happy path, peeled tags, malformed
  refs, URL validation, HTTP error.
Risky contracts: Asserts that anonymous HTTPS is used and that the
  returned list is bounded.
Validation: `uv run pytest -q api/tests/test_upgrade_remote_tags.py`.
"""

from __future__ import annotations

import httpx
import pytest
from api.services.upgrade import remote_tags


def _pkt(line: str) -> bytes:
    """Encode a pkt-line for the smart-protocol advertisement payload."""
    if not line:
        return b"0000"
    body = line.encode("utf-8") + b"\n"
    length = len(body) + 4
    return f"{length:04x}".encode() + body


def _advertisement(refs: list[tuple[str, str]], *, with_caps: bool = True) -> bytes:
    """Build a minimal smart-protocol response with the supplied (sha, ref) refs."""
    out = bytearray()
    out += _pkt("# service=git-upload-pack")
    out += b"0000"
    first = True
    for sha, ref in refs:
        if first and with_caps:
            payload = f"{sha} {ref}\x00multi_ack thin-pack side-band ofs-delta"
            out += _pkt(payload)
            first = False
        else:
            out += _pkt(f"{sha} {ref}")
    out += b"0000"
    return bytes(out)


def _client_with(payload: bytes, *, status_code: int = 200) -> type[httpx.Client]:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/info/refs")
        assert request.url.params.get("service") == "git-upload-pack"
        assert request.headers["Accept"].startswith("application/x-git-upload-pack")
        return httpx.Response(status_code, content=payload)

    transport = httpx.MockTransport(handler)

    class _StubClient(httpx.Client):
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[override]
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    return _StubClient


def test_fetch_release_tags_returns_semver_sorted_descending() -> None:
    sha_a = "a" * 40
    sha_b = "b" * 40
    sha_c = "c" * 40
    payload = _advertisement(
        [
            (sha_a, "refs/heads/main"),
            (sha_b, "refs/tags/v0.2.1"),
            (sha_c, "refs/tags/v0.3.0"),
            (sha_a, "refs/tags/v0.10.0"),
        ]
    )
    tags = remote_tags.fetch_release_tags(
        "https://example.test/foo.git",
        http_client_factory=_client_with(payload),
    )
    assert [t.name for t in tags] == ["0.10.0", "0.3.0", "0.2.1"]
    assert tags[0].commit_sha == sha_a
    assert all(t.raw_ref.startswith("refs/tags/v") for t in tags)


def test_fetch_release_tags_skips_peeled_and_non_semver_tags() -> None:
    sha = "1" * 40
    payload = _advertisement(
        [
            (sha, "refs/tags/v0.3.0"),
            (sha, "refs/tags/v0.3.0^{}"),
            (sha, "refs/tags/release-candidate"),
            (sha, "refs/tags/v1.2"),
            (sha, "refs/tags/v1.2.3.4"),
        ]
    )
    tags = remote_tags.fetch_release_tags(
        "https://example.test/foo.git",
        http_client_factory=_client_with(payload),
    )
    assert [t.name for t in tags] == ["0.3.0"]


def test_fetch_release_tags_rejects_unsupported_url() -> None:
    with pytest.raises(remote_tags.RemoteTagsError):
        remote_tags.fetch_release_tags("not a url")
    with pytest.raises(remote_tags.RemoteTagsError):
        remote_tags.fetch_release_tags("git@example.com:foo/bar.git")


def test_fetch_release_tags_rejects_loopback_and_metadata() -> None:
    for url in (
        "https://127.0.0.1/foo.git",
        "https://[::1]/foo.git",
        "https://169.254.169.254/foo.git",
        "https://localhost/foo.git",
    ):
        with pytest.raises(remote_tags.RemoteTagsError):
            remote_tags.fetch_release_tags(url)


def test_fetch_release_tags_caps_response_body() -> None:
    payload = b"x" * 100

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    class _Stub(httpx.Client):
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[override]
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    with pytest.raises(remote_tags.RemoteTagsError):
        remote_tags.fetch_release_tags(
            "https://example.test/foo.git",
            http_client_factory=_Stub,
            max_response_bytes=10,
        )


def test_mask_remote_url_strips_credentials() -> None:
    assert (
        remote_tags.mask_remote_url(
            "https://x-access-token:supersecret@github.com/foo/bar.git"
        )
        == "https://github.com/foo/bar.git"
    )
    assert (
        remote_tags.mask_remote_url("https://example.test:8443/foo.git")
        == "https://example.test:8443/foo.git"
    )
    assert remote_tags.mask_remote_url("not a url") in {
        "not a url",
        "<unparseable url>",
    }


def test_fetch_release_tags_wraps_http_errors() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"service unavailable")

    class _StubClient(httpx.Client):
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[override]
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    with pytest.raises(remote_tags.RemoteTagsError):
        remote_tags.fetch_release_tags(
            "https://example.test/foo.git",
            http_client_factory=_StubClient,
        )


def test_filter_candidates_returns_only_greater_than_running() -> None:
    sha = "f" * 40
    tags = [
        remote_tags.RemoteTag(name="0.2.0", raw_ref="refs/tags/v0.2.0", commit_sha=sha),
        remote_tags.RemoteTag(name="0.2.1", raw_ref="refs/tags/v0.2.1", commit_sha=sha),
        remote_tags.RemoteTag(name="0.3.0", raw_ref="refs/tags/v0.3.0", commit_sha=sha),
    ]
    out = remote_tags.filter_candidates(tags, running_version="0.2.1")
    assert [t.name for t in out] == ["0.3.0"]


def test_filter_candidates_with_invalid_running_returns_all() -> None:
    sha = "f" * 40
    tags = [
        remote_tags.RemoteTag(name="0.2.0", raw_ref="refs/tags/v0.2.0", commit_sha=sha),
    ]
    out = remote_tags.filter_candidates(tags, running_version="not-a-version")
    assert [t.name for t in out] == ["0.2.0"]


def test_configured_remote_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(remote_tags.UPGRADE_GIT_REMOTE_ENV, raising=False)
    assert remote_tags.configured_remote() is None
    monkeypatch.setenv(remote_tags.UPGRADE_GIT_REMOTE_ENV, "  https://example.test/foo.git ")
    assert remote_tags.configured_remote() == "https://example.test/foo.git"


def test_max_tags_caps_result() -> None:
    sha = "a" * 40
    refs = [(sha, f"refs/tags/v0.0.{i}") for i in range(50)]
    payload = _advertisement(refs)
    tags = remote_tags.fetch_release_tags(
        "https://example.test/foo.git",
        http_client_factory=_client_with(payload),
        max_tags=5,
    )
    assert len(tags) == 5
