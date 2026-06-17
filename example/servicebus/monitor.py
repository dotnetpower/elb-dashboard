#!/usr/bin/env python3
"""Monitoring example — read Service Bus runtime counts and peek messages.

Single-file, standalone reproduction of how the dashboard monitors the optional
Service Bus integration (``api.services.service_bus.entity_counts`` +
``peek_requests`` / ``peek_dead_letter``). It surfaces the same numbers the
Message Flow card and the Settings → Service Bus panel render:

* request queue: active / dead-letter / scheduled / total message counts, plus
  the additive telemetry block (capacity, transfer counters, entity status,
  created/updated/accessed timestamps);
* completion topic: per-subscription active / dead-letter / transfer counts;
* a non-destructive peek of the first few request-queue (and DLQ) messages so
  you can see the live request JSON without consuming it.

Auth (Entra ``DefaultAzureCredential``):
* the counts call uses the management plane and needs ``Azure Service Bus Data
  Owner`` (Manage / EntityRead);
* the peek uses the data plane and needs only ``Azure Service Bus Data
  Receiver``. ``--peek-only`` skips the management call so a Receiver-only
  identity still works.

Usage:
    python monitor.py --self-test          # offline: shape functions only
    python monitor.py                      # full snapshot (counts + peek)
    python monitor.py --peek-only          # data-plane only (Receiver role)
    python monitor.py --peek 10            # peek more request messages
    watch -n 5 python monitor.py --peek 0  # poll counts every 5s
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC
from typing import Any

NAMESPACE_FQDN = os.environ.get(
    "SERVICEBUS_NAMESPACE_FQDN", "sb-elb-dashboard-krc.servicebus.windows.net"
)
REQUEST_QUEUE = os.environ.get("SERVICEBUS_REQUEST_QUEUE", "elastic-blast-requests")
COMPLETION_TOPIC = os.environ.get("SERVICEBUS_COMPLETION_TOPIC", "elastic-blast-completions")

# Cap a peeked body preview so a large query FASTA cannot flood the terminal.
_PEEK_BODY_MAX_CHARS = 4000


def _iso_or_none(value: Any) -> str | None:
    """Render an SDK datetime as ISO-8601 with a Z suffix, tolerant of None."""
    if value is None:
        return None
    try:
        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def shape_queue_counts(runtime: Any, static: Any | None) -> dict[str, Any]:
    """Shape queue runtime + static properties into the dashboard counts dict.

    Mirrors ``entity_counts``'s ``queue`` block: the four counters the SPA has
    always rendered plus the additive ``telemetry`` block. Every telemetry field
    is best-effort — a missing SDK attribute degrades to ``None`` so an SDK bump
    never breaks the counts contract.
    """
    size_in_bytes = getattr(runtime, "size_in_bytes", None)
    max_size_in_mb = getattr(static, "max_size_in_megabytes", None) if static else None
    size_pct: float | None = None
    if (
        isinstance(size_in_bytes, int)
        and isinstance(max_size_in_mb, int)
        and max_size_in_mb > 0
    ):
        size_pct = round(size_in_bytes / (max_size_in_mb * 1024 * 1024) * 100, 2)

    return {
        "active_message_count": getattr(runtime, "active_message_count", None),
        "dead_letter_message_count": getattr(runtime, "dead_letter_message_count", None),
        "scheduled_message_count": getattr(runtime, "scheduled_message_count", None),
        "total_message_count": getattr(runtime, "total_message_count", None),
        "telemetry": {
            "size_in_bytes": size_in_bytes,
            "max_size_in_mb": max_size_in_mb,
            "size_pct": size_pct,
            "transfer_message_count": getattr(runtime, "transfer_message_count", None),
            "transfer_dead_letter_message_count": getattr(
                runtime, "transfer_dead_letter_message_count", None
            ),
            "status": str(getattr(static, "status", "") or "") if static else None,
            "created_at": _iso_or_none(getattr(runtime, "created_at_utc", None)),
            "updated_at": _iso_or_none(getattr(runtime, "updated_at_utc", None)),
            "accessed_at": _iso_or_none(getattr(runtime, "accessed_at_utc", None)),
        },
    }


def shape_subscription_counts(runtime: Any, name: str) -> dict[str, Any]:
    """Shape one completion-topic subscription's runtime counts."""
    return {
        "name": name,
        "active_message_count": getattr(runtime, "active_message_count", None),
        "dead_letter_message_count": getattr(runtime, "dead_letter_message_count", None),
        "transfer_message_count": getattr(runtime, "transfer_message_count", None),
        "transfer_dead_letter_message_count": getattr(
            runtime, "transfer_dead_letter_message_count", None
        ),
    }


def shape_peek_preview(message: Any) -> dict[str, Any]:
    """Shape a peeked message into a size-bounded, JSON-safe preview.

    Parses the body as JSON (the request contract) when possible, otherwise
    keeps the raw text. Truncates a large body and flags it with
    ``body_truncated`` so a big query FASTA cannot flood the output.
    """
    try:
        raw = b"".join(message.body).decode("utf-8", "replace")
    except Exception:
        raw = str(message)
    truncated = len(raw) > _PEEK_BODY_MAX_CHARS
    raw_preview = raw[:_PEEK_BODY_MAX_CHARS]
    try:
        parsed: Any = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = None
    return {
        "message_id": getattr(message, "message_id", None),
        "correlation_id": getattr(message, "correlation_id", None),
        "subject": getattr(message, "subject", None),
        "enqueued_time_utc": _iso_or_none(getattr(message, "enqueued_time_utc", None)),
        "sequence_number": getattr(message, "sequence_number", None),
        "delivery_count": getattr(message, "delivery_count", None),
        "dead_letter_reason": getattr(message, "dead_letter_reason", None),
        "body": parsed if parsed is not None else raw_preview,
        "body_truncated": truncated,
    }


def read_counts() -> dict[str, Any]:
    """Read runtime counts via the management plane (needs Data Owner)."""
    from azure.identity import DefaultAzureCredential
    from azure.servicebus.management import ServiceBusAdministrationClient

    result: dict[str, Any] = {"queue": None, "dead_letter": None, "subscriptions": []}
    with ServiceBusAdministrationClient(
        NAMESPACE_FQDN, DefaultAzureCredential()
    ) as admin:
        runtime = admin.get_queue_runtime_properties(REQUEST_QUEUE)
        static = None
        try:
            static = admin.get_queue(REQUEST_QUEUE)
        except Exception:
            static = None
        result["queue"] = shape_queue_counts(runtime, static)
        result["dead_letter"] = getattr(runtime, "dead_letter_message_count", None)
        if COMPLETION_TOPIC:
            try:
                for sub in admin.list_subscriptions(COMPLETION_TOPIC):
                    srt = admin.get_subscription_runtime_properties(
                        COMPLETION_TOPIC, sub.name
                    )
                    result["subscriptions"].append(
                        shape_subscription_counts(srt, sub.name)
                    )
            except Exception as exc:  # subscription listing is best-effort
                print(
                    f"subscription counts unavailable: {type(exc).__name__}",
                    file=sys.stderr,
                )
    return result


def peek_messages(max_count: int, dead_letter: bool) -> list[dict[str, Any]]:
    """Non-destructive peek of the request queue (or its DLQ)."""
    from azure.identity import DefaultAzureCredential
    from azure.servicebus import ServiceBusClient, ServiceBusSubQueue

    out: list[dict[str, Any]] = []
    count = max(1, min(max_count, 100))
    kwargs: dict[str, Any] = {}
    if dead_letter:
        kwargs["sub_queue"] = ServiceBusSubQueue.DEAD_LETTER
    with ServiceBusClient(NAMESPACE_FQDN, DefaultAzureCredential()) as client:
        with client.get_queue_receiver(REQUEST_QUEUE, **kwargs) as receiver:
            for message in receiver.peek_messages(max_message_count=count):
                out.append(shape_peek_preview(message))
    return out


class _FakeRuntime:
    """Stand-in for an SDK QueueRuntimeProperties for the offline self-test."""

    active_message_count = 3
    dead_letter_message_count = 1
    scheduled_message_count = 0
    total_message_count = 4
    size_in_bytes = 2048
    transfer_message_count = 0
    transfer_dead_letter_message_count = 0
    created_at_utc = None
    updated_at_utc = None
    accessed_at_utc = None


class _FakeStatic:
    max_size_in_megabytes = 1024
    status = "Active"


def _self_test() -> int:
    """Offline check of the shaping functions — no Azure, no network."""
    counts = shape_queue_counts(_FakeRuntime(), _FakeStatic())
    assert counts["active_message_count"] == 3
    assert counts["dead_letter_message_count"] == 1
    assert counts["total_message_count"] == 4
    assert counts["telemetry"]["max_size_in_mb"] == 1024
    assert counts["telemetry"]["size_pct"] == round(2048 / (1024 * 1024 * 1024) * 100, 2)
    assert counts["telemetry"]["status"] == "Active"

    # Missing static properties must degrade gracefully (Receiver-only path).
    degraded = shape_queue_counts(_FakeRuntime(), None)
    assert degraded["telemetry"]["max_size_in_mb"] is None
    assert degraded["telemetry"]["size_pct"] is None
    assert degraded["telemetry"]["status"] is None

    sub = shape_subscription_counts(_FakeRuntime(), "default")
    assert sub == {
        "name": "default",
        "active_message_count": 3,
        "dead_letter_message_count": 1,
        "transfer_message_count": 0,
        "transfer_dead_letter_message_count": 0,
    }

    # The peek preview parses a request body as JSON and bounds its size.
    class _FakeMsg:
        body = (b'{"program":"blastn","db":"core_nt","external_correlation_id":"c1"}',)
        message_id = "m1"
        correlation_id = "c1"
        subject = "blast.request"
        enqueued_time_utc = None
        sequence_number = 7
        delivery_count = 1
        dead_letter_reason = None

    preview = shape_peek_preview(_FakeMsg())
    assert preview["message_id"] == "m1"
    assert preview["subject"] == "blast.request"
    assert preview["body"]["program"] == "blastn"
    assert preview["body_truncated"] is False

    print("self-test OK: counts + subscription + peek shaping match the dashboard")
    print(json.dumps({"queue": counts, "subscriptions": [sub], "peek": [preview]}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--peek",
        type=int,
        default=5,
        help="How many request-queue messages to peek (0 = skip the peek).",
    )
    parser.add_argument(
        "--peek-only",
        action="store_true",
        help="Skip the management-plane counts call (Data Receiver role only).",
    )
    parser.add_argument(
        "--dlq",
        action="store_true",
        help="Also peek the dead-letter sub-queue.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Validate the shaping functions offline (no Azure).",
    )
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    snapshot: dict[str, Any] = {
        "namespace": NAMESPACE_FQDN,
        "request_queue": REQUEST_QUEUE,
        "completion_topic": COMPLETION_TOPIC,
    }

    if not args.peek_only:
        try:
            snapshot["counts"] = read_counts()
        except Exception as exc:
            snapshot["counts_error"] = f"{type(exc).__name__}: {exc}"
            print(
                "counts unavailable (needs Azure Service Bus Data Owner): "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )

    if args.peek > 0:
        try:
            snapshot["request_peek"] = peek_messages(args.peek, dead_letter=False)
            if args.dlq:
                snapshot["dead_letter_peek"] = peek_messages(args.peek, dead_letter=True)
        except Exception as exc:
            snapshot["peek_error"] = f"{type(exc).__name__}: {exc}"
            print(f"peek failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    print(json.dumps(snapshot, indent=2, default=str))
    # Non-zero exit only when nothing could be read at all.
    if "counts" not in snapshot and "request_peek" not in snapshot:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
