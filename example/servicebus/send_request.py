#!/usr/bin/env python3
"""Producer example — enqueue a BLAST request onto the Service Bus request queue.

This is a single-file, standalone reproduction of how the dashboard producer
side (``api.services.service_bus.send_request``) puts a message on the
``elastic-blast-requests`` queue. The JSON body and the message envelope are
byte-for-byte the same contract the worker drain (``drain_and_resubmit``)
consumes, so a message sent by this script is processed exactly like a real
dashboard submit.

Two body shapes are supported, mirroring the two consumer routing paths:

* ``--mode xml`` (default) → the XML-locked ``/api/v1/elastic-blast/submit``
  contract. ``options.outfmt`` is fixed to ``5`` (BLAST XML) because the
  external result pipeline rebuilds FASTA from ``Hsp_hseq``.
* ``--mode v1`` → the free-form ``/v1/jobs`` contract carrying ``blast_options``
  with a multi-token tabular ``outfmt`` such as ``"7 std staxids sstrand qseq
  sseq"``. A body carrying ``blast_options`` is routed to the sibling
  ``POST /v1/jobs`` by the consumer.

Envelope (identical to the live producer):
    content_type = "application/json"
    subject      = "blast.request"
    correlation_id = <external_correlation_id>

Auth: ``DefaultAzureCredential`` (interactive ``az login`` or a managed
identity) needs the ``Azure Service Bus Data Sender`` role on the namespace.

Usage:
    # Validate the body + envelope offline (no Azure, no network):
    python send_request.py --self-test

    # Build and print the message without sending it:
    python send_request.py --dry-run

    # Actually enqueue (needs az login + Data Sender role):
    python send_request.py --db core_nt --program blastn

    # Multi-token tabular request on the /v1/jobs path:
    python send_request.py --mode v1 --outfmt "7 std staxids sstrand qseq sseq"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

NAMESPACE_FQDN = os.environ.get(
    "SERVICEBUS_NAMESPACE_FQDN", "sb-elb-dashboard-krc.servicebus.windows.net"
)
REQUEST_QUEUE = os.environ.get("SERVICEBUS_REQUEST_QUEUE", "elastic-blast-requests")

DEFAULT_QUERY_FASTA = ">query1\nACGTACGTACGTACGTACGTACGTACGTACGTACGTACGT\n"


def build_request_body(args: argparse.Namespace) -> dict:
    """Build the exact request JSON body the dashboard producer enqueues.

    The body matches ``ExternalBlastSubmitRequest`` (mode ``xml``) or
    ``ExternalBlastV1Request`` (mode ``v1``). A server-derived
    ``external_correlation_id`` is generated when the caller did not supply one,
    matching the live ``/settings/service-bus/send`` route behaviour.
    """
    correlation_id = (args.correlation_id or uuid.uuid4().hex).strip()

    body: dict = {
        "program": args.program,
        "db": args.db,
        "query_fasta": args.query_fasta,
        "resource_profile": args.resource_profile,
        "external_correlation_id": correlation_id,
    }
    if args.taxid is not None:
        body["taxid"] = args.taxid
        # is_inclusive only carries meaning together with a taxid filter.
        body["is_inclusive"] = args.is_inclusive

    if args.mode == "v1":
        # Free-form /v1/jobs options (multi-token tabular outfmt + raw extra).
        blast_options: dict = {}
        if args.outfmt:
            blast_options["outfmt"] = args.outfmt
        if args.evalue is not None:
            blast_options["evalue"] = args.evalue
        if args.max_target_seqs is not None:
            blast_options["max_target_seqs"] = args.max_target_seqs
        if args.extra:
            blast_options["extra"] = args.extra
        if args.searchsp is not None:
            blast_options["db_effective_search_space"] = args.searchsp
        body["blast_options"] = blast_options
    else:
        # XML-locked /api/v1/elastic-blast/submit options (outfmt fixed to 5).
        body["options"] = {
            "outfmt": 5,
            "word_size": args.word_size,
            "dust": args.dust,
            "evalue": args.evalue if args.evalue is not None else 0.05,
            "max_target_seqs": args.max_target_seqs
            if args.max_target_seqs is not None
            else 500,
        }

    # Caller-supplied pass-through value echoed onto every completion event.
    if args.request_id:
        body["request_id"] = args.request_id
    return body


def build_envelope(body: dict) -> dict:
    """Return the message envelope fields the live producer sets.

    Mirrors ``ServiceBusMessage(content_type=..., subject=..., correlation_id=...)``
    in ``api.services.service_bus.send_request``.
    """
    return {
        "content_type": "application/json",
        "subject": "blast.request",
        "correlation_id": str(body.get("external_correlation_id") or "") or None,
    }


def _send(body: dict) -> str:
    """Enqueue the body onto the request queue. Returns the message id used."""
    from azure.identity import DefaultAzureCredential
    from azure.servicebus import ServiceBusClient, ServiceBusMessage

    envelope = build_envelope(body)
    message = ServiceBusMessage(
        json.dumps(body, default=str),
        content_type=envelope["content_type"],
        subject=envelope["subject"],
        correlation_id=envelope["correlation_id"],
    )
    with ServiceBusClient(NAMESPACE_FQDN, DefaultAzureCredential()) as client:
        with client.get_queue_sender(REQUEST_QUEUE) as sender:
            sender.send_messages(message)
    return message.message_id or ""


def _self_test() -> int:
    """Offline structural check — no Azure, no network.

    Builds both body shapes, round-trips them through JSON, and asserts the
    contract the consumer relies on (required fields, fixed XML outfmt, the v1
    blast_options routing key, and the envelope correlation binding).
    """
    ns = argparse.Namespace(
        program="blastn",
        db="core_nt",
        query_fasta=DEFAULT_QUERY_FASTA,
        resource_profile="standard",
        correlation_id="self-test-corr",
        taxid=9606,
        is_inclusive=True,
        word_size=28,
        dust=True,
        evalue=None,
        max_target_seqs=None,
        outfmt="7 std staxids sstrand qseq sseq",
        extra=None,
        request_id="caller-123",
        mode="xml",
    )

    xml_body = build_request_body(ns)
    roundtrip = json.loads(json.dumps(xml_body, default=str))
    assert roundtrip["program"] == "blastn"
    assert roundtrip["db"] == "core_nt"
    assert roundtrip["query_fasta"].startswith(">")
    assert roundtrip["external_correlation_id"] == "self-test-corr"
    assert roundtrip["taxid"] == 9606
    assert roundtrip["is_inclusive"] is True
    assert roundtrip["options"]["outfmt"] == 5, "XML path must pin outfmt to 5"
    assert roundtrip["options"]["word_size"] == 28
    assert roundtrip["request_id"] == "caller-123"
    assert "blast_options" not in roundtrip

    env = build_envelope(xml_body)
    assert env["content_type"] == "application/json"
    assert env["subject"] == "blast.request"
    assert env["correlation_id"] == "self-test-corr"

    ns.mode = "v1"
    v1_body = build_request_body(ns)
    v1_roundtrip = json.loads(json.dumps(v1_body, default=str))
    assert "options" not in v1_roundtrip, "v1 path must not emit the XML options object"
    assert v1_roundtrip["blast_options"]["outfmt"] == "7 std staxids sstrand qseq sseq"
    assert build_envelope(v1_body)["subject"] == "blast.request"

    print("self-test OK: request body + envelope match the live producer contract")
    print("--- xml mode body ---")
    print(json.dumps(xml_body, indent=2))
    print("--- v1 mode body ---")
    print(json.dumps(v1_body, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("xml", "v1"),
        default="xml",
        help="xml = /api/v1/elastic-blast/submit (outfmt 5), v1 = /v1/jobs (free-form outfmt)",
    )
    parser.add_argument("--program", default="blastn")
    parser.add_argument("--db", default="core_nt")
    parser.add_argument("--query-fasta", dest="query_fasta", default=DEFAULT_QUERY_FASTA)
    parser.add_argument("--resource-profile", dest="resource_profile", default="standard")
    parser.add_argument("--correlation-id", dest="correlation_id", default=None)
    parser.add_argument("--request-id", dest="request_id", default=None)
    parser.add_argument("--taxid", type=int, default=None)
    parser.add_argument(
        "--is-inclusive",
        dest="is_inclusive",
        action="store_true",
        default=True,
        help="When a taxid is set, include (True) vs exclude (False) it.",
    )
    parser.add_argument(
        "--exclusive",
        dest="is_inclusive",
        action="store_false",
        help="Treat the taxid as a negative (exclude) filter.",
    )
    parser.add_argument("--word-size", dest="word_size", type=int, default=28)
    parser.add_argument("--dust", action="store_true", default=True)
    parser.add_argument("--no-dust", dest="dust", action="store_false")
    parser.add_argument("--evalue", type=float, default=None)
    parser.add_argument("--max-target-seqs", dest="max_target_seqs", type=int, default=None)
    parser.add_argument(
        "--outfmt",
        default="7 std staxids sstrand qseq sseq",
        help="v1 mode only: multi-token tabular outfmt string.",
    )
    parser.add_argument("--extra", default=None, help="v1 mode only: raw CLI flags.")
    parser.add_argument(
        "--searchsp",
        dest="searchsp",
        type=int,
        default=None,
        help=(
            "v1 mode only: calibrated Web BLAST effective search space "
            "(db_effective_search_space). Omit to let the consumer apply the "
            "calibrated value automatically for a known DB."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and print the message without enqueueing.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Validate the body/envelope contract offline (no Azure).",
    )
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    body = build_request_body(args)
    envelope = build_envelope(body)
    print(f"namespace : {NAMESPACE_FQDN}")
    print(f"queue     : {REQUEST_QUEUE}")
    print(f"envelope  : {json.dumps(envelope)}")
    print(f"body      : {json.dumps(body, indent=2)}")

    if args.dry_run:
        print("\ndry-run: message NOT sent.")
        return 0

    try:
        message_id = _send(body)
    except Exception as exc:
        print(f"\nsend failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"\nqueued: message_id={message_id} corr={body['external_correlation_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
