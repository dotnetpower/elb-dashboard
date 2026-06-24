#!/usr/bin/env python3
"""Load-test producer — enqueue 500-1000 BLAST requests onto the request queue.

This drives the Service Bus request-queue load pattern from the integration
notes (500-1000 requests in a burst) so the drain throughput, DLQ behaviour, and
openapi memory headroom can be measured end-to-end. It reuses the EXACT body +
envelope contract from ``send_request.py`` so every message is processed like a
real dashboard submit; it only adds batching, a shared run tag, and a timing
report.

SAFETY (charter §13 "Load / performance testing" — NON-NEGOTIABLE):
  * This script NEVER creates Azure resources and NEVER mutates shared config.
    It only SENDS messages to the EXISTING namespace/queue resolved from the
    environment. There is nothing to tear down because nothing is provisioned.
  * Point it at the namespace already configured for the environment
    (``SERVICEBUS_NAMESPACE_FQDN`` / ``SERVICEBUS_REQUEST_QUEUE``). Do NOT stand
    up a throwaway namespace and do NOT repoint the dashboard's Service Bus
    config row at a test target.
  * Prefer a small DB / short query so the cluster cost of N concurrent BLAST
    runs stays bounded; the point is to measure QUEUE + DRAIN throughput, not to
    run heavy science.

Auth: ``DefaultAzureCredential`` (interactive ``az login`` or a managed
identity) needs the ``Azure Service Bus Data Sender`` role on the namespace.

Usage:
    # Offline structural check — no Azure, no network:
    python load_test.py --self-test

    # Build the batch and print a summary WITHOUT sending:
    python load_test.py --count 500 --dry-run

    # Actually enqueue 500 requests (needs az login + Data Sender role):
    python load_test.py --count 500 --db core_nt --program blastn

Each message gets a unique ``external_correlation_id`` of the form
``<run_tag>-<index>`` so a consumer / the dashboard can correlate the whole
burst back to one run (the ``request_id`` pass-through carries ``<run_tag>``).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

# Reuse the live producer contract from the sibling example so the load body is
# byte-for-byte what the dashboard enqueues.
from send_request import (  # type: ignore[import-not-found]
    DEFAULT_QUERY_FASTA,
    NAMESPACE_FQDN,
    REQUEST_QUEUE,
    build_envelope,
    build_request_body,
)

# Service Bus caps a single batch at 256 KiB (Standard); our bodies are ~1 KiB,
# so 100 per batch stays well under the limit while keeping round-trips low.
_BATCH_SIZE = 100


def _body_for_index(args: argparse.Namespace, run_tag: str, index: int) -> dict:
    """Build one request body with a unique, run-correlated correlation id."""
    ns = argparse.Namespace(
        mode=args.mode,
        program=args.program,
        db=args.db,
        query_fasta=args.query_fasta,
        resource_profile=args.resource_profile,
        correlation_id=f"{run_tag}-{index:04d}",
        request_id=run_tag,
        taxid=args.taxid,
        is_inclusive=args.is_inclusive,
        word_size=args.word_size,
        dust=args.dust,
        evalue=args.evalue,
        max_target_seqs=args.max_target_seqs,
        outfmt=args.outfmt,
        extra=args.extra,
        searchsp=args.searchsp,
    )
    return build_request_body(ns)


def _send_batch(bodies: list[dict]) -> int:
    """Send all bodies to the request queue in chunks. Returns the count sent."""
    from azure.identity import DefaultAzureCredential
    from azure.servicebus import ServiceBusClient, ServiceBusMessage

    sent = 0
    with ServiceBusClient(NAMESPACE_FQDN, DefaultAzureCredential()) as client:
        with client.get_queue_sender(REQUEST_QUEUE) as sender:
            for start in range(0, len(bodies), _BATCH_SIZE):
                chunk = bodies[start : start + _BATCH_SIZE]
                messages = []
                for body in chunk:
                    envelope = build_envelope(body)
                    messages.append(
                        ServiceBusMessage(
                            json.dumps(body, default=str),
                            content_type=envelope["content_type"],
                            subject=envelope["subject"],
                            correlation_id=envelope["correlation_id"],
                        )
                    )
                sender.send_messages(messages)
                sent += len(messages)
                print(f"  sent {sent}/{len(bodies)}", flush=True)
    return sent


def _self_test() -> int:
    """Offline check — build N bodies, assert unique corr ids + shared run tag."""
    args = argparse.Namespace(
        mode="xml",
        program="blastn",
        db="core_nt",
        query_fasta=DEFAULT_QUERY_FASTA,
        resource_profile="standard",
        taxid=None,
        is_inclusive=True,
        word_size=28,
        dust=True,
        evalue=None,
        max_target_seqs=None,
        outfmt="7 std staxids sstrand qseq sseq",
        extra=None,
        searchsp=None,
    )
    run_tag = "lt-selftest"
    n = 500
    bodies = [_body_for_index(args, run_tag, i) for i in range(n)]
    corr_ids = {b["external_correlation_id"] for b in bodies}
    assert len(corr_ids) == n, "every message must have a unique correlation id"
    assert all(b["request_id"] == run_tag for b in bodies), "run tag rides request_id"
    assert all(b["options"]["outfmt"] == 5 for b in bodies), "xml path pins outfmt 5"
    # v1 mode shape check.
    args.mode = "v1"
    v1 = _body_for_index(args, run_tag, 0)
    assert "blast_options" in v1 and "options" not in v1
    print(f"self-test OK: built {n} unique-correlated bodies + envelope contract")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--count",
        type=int,
        default=500,
        help="Number of requests to enqueue (the notes target 500-1000).",
    )
    parser.add_argument("--mode", choices=("xml", "v1"), default="xml")
    parser.add_argument("--program", default="blastn")
    parser.add_argument("--db", default="core_nt")
    parser.add_argument("--query-fasta", dest="query_fasta", default=DEFAULT_QUERY_FASTA)
    parser.add_argument("--resource-profile", dest="resource_profile", default="standard")
    parser.add_argument("--taxid", type=int, default=None)
    parser.add_argument("--is-inclusive", dest="is_inclusive", action="store_true", default=True)
    parser.add_argument("--exclusive", dest="is_inclusive", action="store_false")
    parser.add_argument("--word-size", dest="word_size", type=int, default=28)
    parser.add_argument("--dust", action="store_true", default=True)
    parser.add_argument("--no-dust", dest="dust", action="store_false")
    parser.add_argument("--evalue", type=float, default=None)
    parser.add_argument("--max-target-seqs", dest="max_target_seqs", type=int, default=None)
    parser.add_argument("--outfmt", default="7 std staxids sstrand qseq sseq")
    parser.add_argument("--extra", default=None)
    parser.add_argument("--searchsp", dest="searchsp", type=int, default=None)
    parser.add_argument("--run-tag", dest="run_tag", default=None, help="Override the run tag.")
    parser.add_argument("--dry-run", action="store_true", help="Build but do not enqueue.")
    parser.add_argument("--self-test", action="store_true", help="Offline contract check.")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    if args.count < 1:
        print("count must be >= 1", file=sys.stderr)
        return 2

    run_tag = (args.run_tag or f"lt-{uuid.uuid4().hex[:8]}").strip()
    bodies = [_body_for_index(args, run_tag, i) for i in range(args.count)]

    print(f"namespace : {NAMESPACE_FQDN}")
    print(f"queue     : {REQUEST_QUEUE}")
    print(f"run_tag   : {run_tag}  (rides request_id on every message)")
    print(f"count     : {args.count}  mode={args.mode} db={args.db} program={args.program}")
    print(f"corr ids  : {run_tag}-0000 .. {run_tag}-{args.count - 1:04d}")

    if args.dry_run:
        print("\ndry-run: nothing sent. Sample body:")
        print(json.dumps(bodies[0], indent=2))
        return 0

    print("\nenqueueing ...", flush=True)
    start = time.monotonic()
    try:
        sent = _send_batch(bodies)
    except Exception as exc:
        print(f"\nsend failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    elapsed = time.monotonic() - start
    rate = sent / elapsed if elapsed > 0 else 0.0
    print(
        f"\ndone: enqueued {sent} requests in {elapsed:.1f}s "
        f"({rate:.0f} msg/s send rate). run_tag={run_tag}"
    )
    print(
        "Measure drain throughput / DLQ from the dashboard Message Flow card or "
        "consume.py; nothing was provisioned, so there is nothing to tear down."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
