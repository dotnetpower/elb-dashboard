#!/usr/bin/env python3
"""Fetch an NCBI Web BLAST result XML by RID for parity fixtures.

Responsibility: Pull a captured-but-not-expired NCBI Web BLAST RID's XML result
into the local parity fixture directory so the comparison harness can run
against an authoritative NCBI reference. This is the opt-in live-mode
counterpart to `api/tests/fixtures/web_blast_parity/`.

Edit boundaries: Operator/dev utility only. Never import from production code
and never execute in CI. The script makes an outbound HTTPS request to
`blast.ncbi.nlm.nih.gov`, so it must remain user-triggered.

Key entry points: `main`, `_poll_for_completion`, `_download_xml`.

Risky contracts: NCBI rate-limits the public BLAST URL API and RIDs expire
after a retention window (typically 36 hours, sometimes shorter). Be polite:
default poll interval is 30s, default per-RID time budget is 30 minutes. Do
not parallel-fetch — the NCBI BLAST URL API explicitly asks clients to poll
sequentially.

Validation: `uv run python scripts/dev/fetch-ncbi-blast-rid.py --help`.
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

NCBI_BLAST_URL = "https://blast.ncbi.nlm.nih.gov/blast/Blast.cgi"
DEFAULT_TOOL = "elb-dashboard-parity"
DEFAULT_EMAIL = "elb-dashboard-parity@example.invalid"


def _http_get(url: str, *, timeout: float = 60.0) -> str:
    req = urllib.request.Request(  # noqa: S310 - hardcoded NCBI URL only.
        url,
        headers={"User-Agent": "elb-dashboard parity-fetcher/1"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - same.
        return resp.read().decode("utf-8", errors="replace")


def _status_url(rid: str, *, tool: str, email: str) -> str:
    params = urllib.parse.urlencode(
        {
            "CMD": "Get",
            "FORMAT_OBJECT": "SearchInfo",
            "RID": rid,
            "TOOL": tool,
            "EMAIL": email,
        }
    )
    return f"{NCBI_BLAST_URL}?{params}"


def _xml_url(rid: str, *, tool: str, email: str) -> str:
    params = urllib.parse.urlencode(
        {
            "CMD": "Get",
            "FORMAT_TYPE": "XML",
            "RID": rid,
            "TOOL": tool,
            "EMAIL": email,
        }
    )
    return f"{NCBI_BLAST_URL}?{params}"


def _parse_status(body: str) -> str:
    """Return one of READY, WAITING, UNKNOWN, FAILED (uppercased)."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("Status="):
            return stripped.split("=", 1)[1].strip().upper()
    return "UNKNOWN"


def _poll_for_completion(
    rid: str,
    *,
    tool: str,
    email: str,
    interval_s: float,
    budget_s: float,
) -> None:
    deadline = time.monotonic() + budget_s
    while True:
        body = _http_get(_status_url(rid, tool=tool, email=email))
        status = _parse_status(body)
        if status == "READY":
            return
        if status in {"UNKNOWN", "FAILED"}:
            raise SystemExit(f"NCBI returned Status={status} for RID {rid}; aborting fetch")
        if time.monotonic() >= deadline:
            raise SystemExit(
                f"RID {rid} did not become READY within {budget_s:.0f}s "
                f"(last status: {status})"
            )
        sys.stderr.write(f"[parity-fetcher] RID {rid} status={status}; sleeping {interval_s}s\n")
        time.sleep(interval_s)


def _download_xml(rid: str, *, tool: str, email: str, out_path: Path) -> int:
    body = _http_get(_xml_url(rid, tool=tool, email=email))
    # NCBI sometimes returns an HTML error page with status 200; require an
    # XML envelope before persisting so a stale fixture isn't overwritten with
    # garbage.
    stripped = body.lstrip()
    if not (stripped.startswith("<?xml") or stripped.startswith("<BlastOutput")):
        raise SystemExit(
            f"NCBI returned a non-XML body for RID {rid}; first 200 chars: {stripped[:200]!r}"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding="utf-8")
    return len(body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rid", required=True, help="NCBI Web BLAST RID, e.g. 1FZVPFJ6014.")
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Destination XML file. Will be overwritten if it exists.",
    )
    parser.add_argument(
        "--tool",
        default=DEFAULT_TOOL,
        help="Value sent as the NCBI BLAST URL API `TOOL` parameter (default: %(default)s).",
    )
    parser.add_argument(
        "--email",
        default=DEFAULT_EMAIL,
        help=(
            "Value sent as the NCBI BLAST URL API `EMAIL` parameter. "
            "Required by NCBI policy when scripting against the public API; "
            "default: %(default)s."
        ),
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=30.0,
        help="Seconds between SearchInfo polls (default: %(default)s).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30 * 60.0,
        help="Total time budget for the fetch, in seconds (default: %(default)s).",
    )
    args = parser.parse_args(argv)

    sys.stderr.write(
        f"[parity-fetcher] polling RID {args.rid} every {args.poll_interval:.0f}s "
        f"(budget {args.timeout:.0f}s)\n"
    )
    _poll_for_completion(
        args.rid,
        tool=args.tool,
        email=args.email,
        interval_s=args.poll_interval,
        budget_s=args.timeout,
    )
    written = _download_xml(args.rid, tool=args.tool, email=args.email, out_path=args.out)
    sys.stderr.write(f"[parity-fetcher] wrote {written} bytes to {args.out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
