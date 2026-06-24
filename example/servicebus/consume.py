#!/usr/bin/env python3
"""Consumer example — receive BLAST request messages from the queue and settle them.

Single-file reproduction of the worker drain
(``api.services.service_bus.drain_requests``): receive request-queue messages,
print a one-line summary, and settle each (complete / abandon / dead-letter).
The real dashboard worker forwards each body to the BLAST OpenAPI plane; this
example just settles so you can watch the queue drain.

Auth: DefaultAzureCredential (``az login``) with the "Azure Service Bus Data
Receiver" role on the namespace.

    python consume.py --max 5                  # receive up to 5, complete each
    python consume.py --max 5 --settle abandon # receive + abandon (redelivered)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

NAMESPACE = os.environ.get(
    "SERVICEBUS_NAMESPACE_FQDN", "sb-elb-dashboard-krc.servicebus.windows.net"
)
QUEUE = os.environ.get("SERVICEBUS_REQUEST_QUEUE", "elastic-blast-requests")
_MAX_WAIT_SECONDS = 5


def parse_body(message: Any) -> dict:
    """Decode the message body (bytes / str / byte-chunk generator) into a dict."""
    raw = message.body
    if not isinstance(raw, (bytes, str)):
        raw = b"".join(raw)  # the SDK yields a generator of byte chunks
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def summarise(body: dict) -> dict:
    """One-line summary + the routing key the worker uses.

    A body carrying ``blast_options`` is routed to the sibling ``POST /v1/jobs``;
    otherwise it goes to the XML-locked ``/api/v1/elastic-blast/submit``.
    """
    return {
        "program": body.get("program"),
        "db": body.get("db"),
        "correlation_id": body.get("external_correlation_id"),
        "route": "v1" if "blast_options" in body else "xml",
    }


def consume(max_messages: int, settle: str) -> dict:
    """Receive up to ``max_messages``, settle each, and return drain stats."""
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
                    print(json.dumps(summarise(parse_body(message))))
                    try:
                        if settle == "complete":
                            receiver.complete_message(message)
                            stats["completed"] += 1
                        elif settle == "dead_letter":
                            receiver.dead_letter_message(message, reason="example")
                            stats["dead_lettered"] += 1
                        else:
                            receiver.abandon_message(message)
                            stats["abandoned"] += 1
                    except Exception as exc:
                        # Lock lost / expired — the broker redelivers. Never raise
                        # so one bad settle does not abort the batch.
                        print(f"settle failed: {type(exc).__name__}", file=sys.stderr)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--max", type=int, default=10, help="max messages to receive")
    parser.add_argument(
        "--settle",
        choices=("complete", "abandon", "dead_letter"),
        default="complete",
    )
    args = parser.parse_args()

    print(f"namespace: {NAMESPACE}")
    print(f"queue:     {QUEUE}")
    stats = consume(args.max, args.settle)
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
