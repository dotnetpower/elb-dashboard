#!/usr/bin/env python3
"""Consumer example — drain the request queue, or read completions and download.

* ``--source requests`` (default): receive request-queue messages, summarise,
  settle. Mirrors ``api.services.service_bus.drain_requests``.
* ``--source completions``: read the ``elastic-blast-completions`` topic and,
  with ``--download``, fetch each ``result_files[].download_url``. Those URLs
  carry a signed ``?token=`` so the download needs no bearer or ``az login``.

Auth: ``DefaultAzureCredential`` with "Azure Service Bus Data Receiver" on the
namespace (for the receive only — downloads use no auth).

    python consume.py --max 5
    python consume.py --source completions --max 5 --download
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

NAMESPACE = os.environ.get(
    "SERVICEBUS_NAMESPACE_FQDN", "sb-elb-dashboard-krc.servicebus.windows.net"
)
QUEUE = os.environ.get("SERVICEBUS_REQUEST_QUEUE", "elastic-blast-requests")
TOPIC = os.environ.get("SERVICEBUS_COMPLETIONS_TOPIC", "elastic-blast-completions")
SUBSCRIPTION = os.environ.get("SERVICEBUS_COMPLETIONS_SUBSCRIPTION", "default")
_MAX_WAIT_SECONDS = 5
_DOWNLOAD_TIMEOUT = 120


def parse_body(message: Any) -> dict:
    """Decode a message body (bytes / str / byte-chunk generator) into a dict."""
    raw = message.body
    if not isinstance(raw, (bytes, str)):
        raw = b"".join(raw)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def settle(receiver: Any, message: Any, mode: str, stats: dict) -> None:
    """Settle one message; never raise so a bad lock cannot abort the batch."""
    try:
        if mode == "complete":
            receiver.complete_message(message)
            stats["completed"] += 1
        elif mode == "dead_letter":
            receiver.dead_letter_message(message, reason="example")
            stats["dead_lettered"] += 1
        else:
            receiver.abandon_message(message)
            stats["abandoned"] += 1
    except Exception as exc:
        print(f"settle failed: {type(exc).__name__}", file=sys.stderr)


def download_file(file_entry: dict, out_dir: Path) -> dict:
    """Fetch one result file by its signed URL — no auth headers."""
    url = str(file_entry.get("download_url") or "")
    name = str(file_entry.get("name") or file_entry.get("file_id") or "result")
    stat = {"name": name, "ok": False, "bytes": 0, "error": None}
    if not url:
        stat["error"] = "missing download_url"
        return stat
    dest = out_dir / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT) as response:  # noqa: S310
            with dest.open("wb") as fh:
                for chunk in iter(lambda: response.read(65536), b""):
                    fh.write(chunk)
                    stat["bytes"] += len(chunk)
        stat["ok"] = True
    except Exception as exc:
        stat["error"] = f"{type(exc).__name__}: {exc}"
        dest.unlink(missing_ok=True)
    return stat


def consume_requests(max_messages: int, mode: str) -> dict:
    """Receive request-queue messages, summarise each, and settle."""
    from azure.identity import DefaultAzureCredential
    from azure.servicebus import ServiceBusClient

    stats = {"received": 0, "completed": 0, "abandoned": 0, "dead_lettered": 0}
    budget = max(1, max_messages)
    with ServiceBusClient(NAMESPACE, DefaultAzureCredential()) as client:
        with client.get_queue_receiver(QUEUE, max_wait_time=_MAX_WAIT_SECONDS) as receiver:
            while budget > 0:
                batch = receiver.receive_messages(
                    max_message_count=min(budget, 32), max_wait_time=_MAX_WAIT_SECONDS
                )
                if not batch:
                    break
                for message in batch:
                    budget -= 1
                    stats["received"] += 1
                    body = parse_body(message)
                    print(json.dumps({
                        "program": body.get("program"),
                        "db": body.get("db"),
                        "correlation_id": body.get("external_correlation_id"),
                        "route": "v1" if "blast_options" in body else "xml",
                    }))
                    settle(receiver, message, mode, stats)
    return stats


def consume_completions(max_messages: int, mode: str, download: bool, out_dir: Path) -> dict:
    """Read completion events; with ``download``, fetch each download_url."""
    from azure.identity import DefaultAzureCredential
    from azure.servicebus import ServiceBusClient

    stats = {"received": 0, "completed": 0, "abandoned": 0, "dead_lettered": 0,
             "files_downloaded": 0, "files_failed": 0}
    budget = max(1, max_messages)
    seen: set[str] = set()
    with ServiceBusClient(NAMESPACE, DefaultAzureCredential()) as client:
        with client.get_subscription_receiver(
            TOPIC, SUBSCRIPTION, max_wait_time=_MAX_WAIT_SECONDS
        ) as receiver:
            while budget > 0:
                batch = receiver.receive_messages(
                    max_message_count=min(budget, 16), max_wait_time=_MAX_WAIT_SECONDS
                )
                if not batch:
                    break
                for message in batch:
                    budget -= 1
                    stats["received"] += 1
                    body = parse_body(message)
                    event_id = str(body.get("event_id") or "")
                    print(json.dumps({
                        "event_id": event_id,
                        "status": body.get("status"),
                        "openapi_job_id": body.get("openapi_job_id"),
                        "result_files": len(body.get("result_files") or []),
                    }))
                    if download and event_id and event_id not in seen:
                        for entry in body.get("result_files") or []:
                            result = download_file(entry, out_dir)
                            print(json.dumps(result))
                            stats["files_downloaded" if result["ok"] else "files_failed"] += 1
                        seen.add(event_id)
                    settle(receiver, message, mode, stats)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source", choices=("requests", "completions"), default="requests")
    parser.add_argument("--max", type=int, default=10, help="max messages to receive")
    parser.add_argument("--settle", choices=("complete", "abandon", "dead_letter"),
                        default="complete")
    parser.add_argument("--download", action="store_true",
                        help="(completions only) fetch every result_files[].download_url")
    parser.add_argument("--out-dir", default="./downloads", help="download target directory")
    args = parser.parse_args()

    print(f"namespace: {NAMESPACE}")
    if args.source == "requests":
        if args.download:
            print("--download requires --source completions", file=sys.stderr)
            return 2
        print(f"queue:     {QUEUE}")
        stats = consume_requests(args.max, args.settle)
    else:
        print(f"topic:     {TOPIC}/{SUBSCRIPTION}")
        out_dir = Path(args.out_dir).expanduser().resolve()
        stats = consume_completions(args.max, args.settle, args.download, out_dir)
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
