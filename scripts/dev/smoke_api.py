#!/usr/bin/env python3
"""End-to-end smoke test for the deployed Container App.

Runs against the live ingress hostname. For endpoints that require an
MSAL bearer token we use a developer ARM token (audience mismatch will
trigger 401 at the api sidecar — we treat that as "endpoint reachable",
which is enough for the smoke). Endpoints that should respond without auth
(health) are checked for actual 200.

Exit code: 0 when every probe passes (200 or expected 401/410/422), non-zero
when any probe is unreachable, returns 5xx, or returns malformed JSON.

Usage:
  python scripts/dev/smoke_api.py
  python scripts/dev/smoke_api.py --url https://ca-elb-control.<...>.io
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass
class Probe:
    name: str
    method: str
    path: str
    expected_status: set[int]  # status codes that mean "endpoint OK"
    body: dict | None = None
    needs_auth: bool = True


PROBES: list[Probe] = [
    # --- public ---
    Probe("health", "GET", "/api/health", {200}, needs_auth=False),
    Probe("openapi", "GET", "/openapi.json", {200, 404}, needs_auth=False),
    # --- auth-protected (401 with ARM token = endpoint reachable, audience check works) ---
    Probe("me", "GET", "/api/me", {200, 401}),
    Probe("monitor.aks", "GET", "/api/monitor/aks?resource_group=rg-elb-ca", {200, 401}),
    Probe("monitor.storage", "GET", "/api/monitor/storage?resource_group=rg-elb-ca&account_name=stelbnm5virmqrdi5c", {200, 401}),
    Probe("monitor.acr", "GET", "/api/monitor/acr?resource_group=rg-elb-ca&registry_name=acrelbnm5virmqrdi5c", {200, 401}),
    Probe("monitor.terminal", "GET", "/api/monitor/terminal", {200, 401}),
    Probe("monitor.jobs", "GET", "/api/monitor/jobs", {200, 401}),
    Probe("arm.subs", "GET", "/api/arm/subscriptions", {200, 401}),
    Probe("arm.rgs", "GET", "/api/arm/subscriptions/00000000-0000-0000-0000-000000000000/resource-groups", {200, 401}),
    Probe("resources.rg", "POST", "/api/resources/ensure-rg", {200, 400, 401, 422}, body={"x": 1}),
    Probe("resources.storage", "POST", "/api/resources/ensure-storage", {200, 400, 401, 422}, body={"x": 1}),
    Probe("resources.acr", "POST", "/api/resources/ensure-acr", {200, 400, 401, 422}, body={"x": 1}),
    Probe("blast.jobs", "GET", "/api/blast/jobs", {200, 401}),
    Probe("blast.databases", "GET", "/api/blast/databases", {200, 401}),
    Probe("blast.schedules", "GET", "/api/blast/schedules", {200, 401}),
    Probe("blast.submit", "POST", "/api/blast/submit", {200, 401, 422}, body={}),
    Probe("aks.skus", "GET", "/api/aks/skus", {200, 401}),
    Probe("aks.provision", "POST", "/api/aks/provision", {200, 401, 422}, body={}),
    Probe("warmup.start", "POST", "/api/warmup/start", {200, 401, 422}, body={}),
    Probe("audit.log", "GET", "/api/audit/log", {200, 401}),
    Probe("terminal.health", "GET", "/api/terminal/health", {200, 401}),
    Probe("terminal.ticket", "POST", "/api/terminal/ticket", {200, 401}, body={}),
    Probe("terminal.legacy.password", "GET", "/api/terminal/foo/password", {401, 410}),
    Probe("terminal.legacy.start", "POST", "/api/terminal/foo/start", {401, 410}),
    # --- frontend reverse proxy (no auth) ---
    Probe("spa.root", "GET", "/", {200}, needs_auth=False),
    Probe("spa.fallback", "GET", "/some/deep/spa/route", {200}, needs_auth=False),
]


def get_arm_token() -> str | None:
    """Get the operator's ARM token via az cli (best-effort)."""
    try:
        out = subprocess.run(
            ["az", "account", "get-access-token", "--query", "accessToken", "-o", "tsv"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        return out.stdout.strip()
    except Exception:
        return None


def run_probe(base_url: str, probe: Probe, token: str | None) -> tuple[bool, str]:
    url = base_url.rstrip("/") + probe.path
    headers = {}
    if probe.needs_auth and token:
        headers["Authorization"] = f"Bearer {token}"
    if probe.body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(probe.body).encode()
    else:
        data = None

    req = Request(url, method=probe.method, headers=headers, data=data)
    t0 = time.monotonic()
    try:
        with urlopen(req, timeout=15) as resp:
            status = resp.status
            body = resp.read(4096)
    except HTTPError as exc:
        status = exc.code
        body = exc.read(4096) if hasattr(exc, "read") else b""
    except URLError as exc:
        return False, f"unreachable: {exc.reason}"
    except Exception as exc:
        return False, f"error: {exc!r}"
    elapsed = (time.monotonic() - t0) * 1000

    if status not in probe.expected_status:
        return False, f"status={status} (expected {sorted(probe.expected_status)}) body={body[:200]!r}"

    # Sanity-check body is JSON for /api routes
    if probe.path.startswith("/api/") and status < 500:
        try:
            json.loads(body or b"{}")
        except Exception:
            return False, f"non-JSON body: {body[:120]!r}"

    return True, f"{status} ({elapsed:.0f}ms)"


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--url",
        default="https://ca-elb-control.gentlemeadow-01289e5b.koreacentral.azurecontainerapps.io",
    )
    args = p.parse_args(argv)

    print(f"=== smoke against {args.url} ===")
    token = get_arm_token()
    if token:
        print(f"  using ARM token (len={len(token)}); api will reject for audience mismatch \u2192 401 expected")
    else:
        print("  no token available; auth-protected endpoints will return 401")

    fails = 0
    for probe in PROBES:
        ok, msg = run_probe(args.url, probe, token)
        marker = "\u2713" if ok else "\u2717"
        print(f"  {marker} {probe.method:6s} {probe.path:60s} {msg}")
        if not ok:
            fails += 1

    print(f"=== {len(PROBES) - fails}/{len(PROBES)} passed ===")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
