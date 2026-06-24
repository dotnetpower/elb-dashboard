#!/usr/bin/env python3
"""Monitoring example — read Service Bus queue counts and peek messages.

Single-file reproduction of the dashboard's Service Bus monitoring
(``api.services.service_bus.entity_counts`` + ``peek_requests``):

  counts : active / dead-letter / scheduled / total message counts
           (management plane, needs "Azure Service Bus Data Owner")
  peek   : non-destructive read of the first few request messages
           (data plane, needs "Azure Service Bus Data Receiver")

    python monitor.py                # counts + peek 5
    python monitor.py --peek 0       # counts only
    python monitor.py --peek-only    # peek only (Receiver-only identity)
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

NAMESPACE = os.environ.get(
    "SERVICEBUS_NAMESPACE_FQDN", "sb-elb-dashboard-krc.servicebus.windows.net"
)
QUEUE = os.environ.get("SERVICEBUS_REQUEST_QUEUE", "elastic-blast-requests")
_PEEK_BODY_MAX_CHARS = 2000


def queue_counts() -> dict:
    """Active / dead-letter / scheduled / total counts via the management plane."""
    from azure.identity import DefaultAzureCredential
    from azure.servicebus.management import ServiceBusAdministrationClient

    with ServiceBusAdministrationClient(NAMESPACE, DefaultAzureCredential()) as admin:
        rt = admin.get_queue_runtime_properties(QUEUE)
    return {
        "active": rt.active_message_count,
        "dead_letter": rt.dead_letter_message_count,
        "scheduled": rt.scheduled_message_count,
        "total": rt.total_message_count,
    }


def peek_requests(count: int) -> list[dict]:
    """Non-destructively peek the first ``count`` request messages."""
    from azure.identity import DefaultAzureCredential
    from azure.servicebus import ServiceBusClient

    out: list[dict] = []
    with ServiceBusClient(NAMESPACE, DefaultAzureCredential()) as client:
        with client.get_queue_receiver(QUEUE) as receiver:
            for message in receiver.peek_messages(max_message_count=count):
                raw = message.body
                if not isinstance(raw, (bytes, str)):
                    raw = b"".join(raw)
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                out.append(
                    {
                        "correlation_id": message.correlation_id,
                        "body": raw[:_PEEK_BODY_MAX_CHARS],
                    }
                )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--peek", type=int, default=5, help="how many messages to peek (0 = none)"
    )
    parser.add_argument(
        "--peek-only",
        action="store_true",
        help="skip counts (works with a Receiver-only identity)",
    )
    args = parser.parse_args()

    print(f"namespace: {NAMESPACE}")
    print(f"queue:     {QUEUE}")
    snapshot: dict[str, Any] = {}
    if not args.peek_only:
        snapshot["counts"] = queue_counts()
    if args.peek > 0:
        snapshot["peek"] = peek_requests(args.peek)
    print(json.dumps(snapshot, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
