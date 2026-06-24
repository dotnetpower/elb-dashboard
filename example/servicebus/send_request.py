#!/usr/bin/env python3
"""Producer example — send one BLAST request to the Service Bus request queue.

The JSON body and message envelope match the dashboard producer
(``api.services.service_bus.send_request``), so a message sent here is drained
and submitted exactly like a real dashboard request.

Two body shapes (the consumer routes on whichever one it sees):
  --mode xml  (default)  ``options.outfmt`` is fixed to 5 (BLAST XML)
  --mode v1              ``blast_options`` with a multi-token tabular outfmt

Auth: DefaultAzureCredential (``az login``) with the "Azure Service Bus Data
Sender" role on the namespace.

    python send_request.py --dry-run             # print the message, don't send
    python send_request.py --db core_nt          # send (needs az login + role)
    python send_request.py --mode v1 --outfmt "7 std staxids"
"""

from __future__ import annotations

import argparse
import json
import os
import uuid

NAMESPACE = os.environ.get(
    "SERVICEBUS_NAMESPACE_FQDN", "sb-elb-dashboard-krc.servicebus.windows.net"
)
QUEUE = os.environ.get("SERVICEBUS_REQUEST_QUEUE", "elastic-blast-requests")
DEFAULT_FASTA = ">query1\nACGTACGTACGTACGTACGTACGTACGTACGTACGTACGT\n"


def build_body(args: argparse.Namespace) -> dict:
    """Build the request JSON body the dashboard producer enqueues."""
    body: dict = {
        "program": args.program,
        "db": args.db,
        "query_fasta": args.query_fasta,
        "external_correlation_id": args.correlation_id or uuid.uuid4().hex,
    }
    if args.taxid is not None:
        body["taxid"] = args.taxid
        body["is_inclusive"] = True
    if args.mode == "v1":
        # /v1/jobs path — free-form tabular outfmt under blast_options.
        body["blast_options"] = {"outfmt": args.outfmt}
    else:
        # /api/v1/elastic-blast/submit path — outfmt locked to 5 (BLAST XML).
        body["options"] = {"outfmt": 5, "evalue": 0.05, "max_target_seqs": 500}
    return body


def send(body: dict) -> None:
    """Enqueue the body onto the request queue with the producer envelope."""
    from azure.identity import DefaultAzureCredential
    from azure.servicebus import ServiceBusClient, ServiceBusMessage

    message = ServiceBusMessage(
        json.dumps(body),
        content_type="application/json",
        subject="blast.request",
        correlation_id=body["external_correlation_id"],
    )
    with ServiceBusClient(NAMESPACE, DefaultAzureCredential()) as client:
        with client.get_queue_sender(QUEUE) as sender:
            sender.send_messages(message)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mode", choices=("xml", "v1"), default="xml")
    parser.add_argument("--program", default="blastn")
    parser.add_argument("--db", default="core_nt")
    parser.add_argument("--query-fasta", dest="query_fasta", default=DEFAULT_FASTA)
    parser.add_argument("--taxid", type=int, default=None)
    parser.add_argument(
        "--outfmt",
        default="7 std staxids sstrand qseq sseq",
        help="v1 mode only: multi-token tabular outfmt string",
    )
    parser.add_argument("--correlation-id", dest="correlation_id", default=None)
    parser.add_argument(
        "--dry-run", action="store_true", help="print the message without sending"
    )
    args = parser.parse_args()

    body = build_body(args)
    print(f"namespace: {NAMESPACE}")
    print(f"queue:     {QUEUE}")
    print(json.dumps(body, indent=2))
    if args.dry_run:
        print("\ndry-run: message NOT sent.")
        return 0
    send(body)
    print(f"\nsent: correlation_id={body['external_correlation_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
