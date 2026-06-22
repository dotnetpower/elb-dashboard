#!/usr/bin/env python3
"""Consumer example — receive and settle Service Bus messages.

Single-file, standalone reproduction of the two consumer roles in the dashboard
Service Bus integration. Pick one with ``--source``:

* ``--source requests`` (default) — receive from the ``elastic-blast-requests``
  queue and SETTLE each message, exactly like the worker drain
  (``api.services.service_bus.drain_requests`` + ``_drain_handler``). It parses
  the request JSON the producer enqueues, decides an action, and completes /
  abandons / dead-letters the message. The loop is BOUNDED by ``--max`` so it
  can never spin forever, and every received message is settled (a leaked lock
  causes redelivery → a duplicate BLAST run).

  NOTE: this is a DEMONSTRATION consumer. It does not submit to the OpenAPI
  plane; it prints the parsed request and completes the message. Run it against
  a throwaway queue, or use ``--peek-settle abandon`` so messages stay on the
  queue for the real worker.

* ``--source completions`` — subscribe to the ``elastic-blast-completions``
  topic and process transition events (``blast.transition``) an external
  observer would consume: ``{event, event_id, attempt, external_correlation_id,
  openapi_job_id, status, ts, result_ref, result_files?, request_id?,
  error_code?}``. A ``succeeded`` event carries ``result_files`` — each with a
  ``download_url`` pointing at the dashboard's authenticated streaming gateway.
  With ``--download`` the consumer calls each ``download_url`` with a bearer
  token and saves the result bytes locally (never a SAS URL; the ``api`` sidecar
  streams the bytes). Subscribers DEDUPE on the stable ``event_id`` (sha256 of
  corr:status) because Service Bus delivery is at-least-once.

Auth (Entra ``DefaultAzureCredential``): both roles need ``Azure Service Bus
Data Receiver`` on the namespace. ``--download`` also needs a dashboard bearer
token — set ``ELB_BEARER_TOKEN`` directly, or ``ELB_API_CLIENT_ID`` so the
script runs ``az account get-access-token --resource <id>`` for you.

  NOTE: the ``ELB_API_CLIENT_ID`` path only works when the API app registration
  has pre-authorized the well-known Azure CLI public client
  (``04b07795-8ddb-461a-bbee-02f9e1bf7b46``) for its ``user_impersonation``
  scope — ``scripts/dev/setup-app-registration.sh`` does this automatically.
  Without it, ``az account get-access-token`` fails with ``AADSTS65001``
  (consent not granted) and the download returns 401. In that case set
  ``ELB_BEARER_TOKEN`` to a token obtained interactively, or ask an admin to
  grant consent.

Usage:
    python consume.py --self-test                       # offline parse/dedupe
    python consume.py --source requests --max 5         # drain up to 5 requests
    python consume.py --source requests --settle abandon  # peek-and-return
    python consume.py --source completions --subscription default --max 10
    ELB_API_CLIENT_ID=<api-client-id> \\
      python consume.py --source completions --download --download-dir ./out
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any

NAMESPACE_FQDN = os.environ.get(
    "SERVICEBUS_NAMESPACE_FQDN", "sb-elb-dashboard-krc.servicebus.windows.net"
)
REQUEST_QUEUE = os.environ.get("SERVICEBUS_REQUEST_QUEUE", "elastic-blast-requests")


def _completion_topic_from_env() -> str:
    if "SERVICEBUS_RESPONSE_TOPIC" in os.environ:
        return os.environ["SERVICEBUS_RESPONSE_TOPIC"].strip()
    if "SERVICEBUS_COMPLETION_TOPIC" in os.environ:
        return os.environ["SERVICEBUS_COMPLETION_TOPIC"].strip()
    return "elastic-blast-completions"


COMPLETION_TOPIC = _completion_topic_from_env()
COMPLETION_SUBSCRIPTION = os.environ.get("SERVICEBUS_COMPLETION_SUBSCRIPTION", "default")


def _completion_kind_from_env() -> str:
    """Completion entity kind: ``topic`` (fan-out) or ``queue`` (point-to-point)."""
    kind = os.environ.get("SERVICEBUS_COMPLETION_KIND", "").strip().lower()
    return kind if kind in {"topic", "queue"} else "topic"


COMPLETION_KIND = _completion_kind_from_env()

# A receive tick is bounded so one run can never block forever.
_RECEIVE_MAX_WAIT_SECONDS = 5


def parse_body(message: Any) -> dict[str, Any]:
    """Best-effort parse a Service Bus message body into a JSON dict.

    The SDK exposes ``message.body`` as ``bytes``/``str`` or a generator of byte
    chunks. Normalise both to text, then JSON. A non-dict or unparseable body
    degrades to an empty dict rather than raising — matching
    ``api.services.service_bus._parse``.
    """
    body = getattr(message, "body", None)
    try:
        if isinstance(body, bytes | bytearray):
            raw = bytes(body).decode("utf-8", "replace")
        elif isinstance(body, str):
            raw = body
        elif body is None:
            raw = ""
        else:
            chunks = [c if isinstance(c, bytes | bytearray) else str(c).encode() for c in body]
            raw = b"".join(chunks).decode("utf-8", "replace")
        parsed = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def summarise_request(body: dict[str, Any]) -> dict[str, Any]:
    """Pull the fields the worker drain cares about out of a request body."""
    has_v1 = isinstance(body.get("blast_options"), dict) and bool(body["blast_options"])
    return {
        "correlation_id": str(body.get("external_correlation_id") or "").strip() or None,
        "request_id": body.get("request_id"),
        "program": body.get("program"),
        "db": body.get("db"),
        # Routing key the live consumer uses: a body carrying blast_options goes
        # to the sibling POST /v1/jobs (free-form outfmt); otherwise the XML path.
        "route": "v1_jobs" if has_v1 else "external_submit",
        "outfmt": (body.get("blast_options") or {}).get("outfmt")
        if has_v1
        else (body.get("options") or {}).get("outfmt"),
    }


def decide_action(summary: dict[str, Any]) -> str:
    """Decide how to settle a request message.

    Mirrors the live drain contract: a message that can never succeed
    (no correlation id) is dead-lettered rather than retried forever; everything
    else completes. ``complete`` / ``abandon`` / ``dead_letter`` map to the SDK
    settlement calls.
    """
    if not summary.get("correlation_id"):
        return "dead_letter"
    return "complete"


def _event_id(correlation_id: str, status: str) -> str:
    """The stable dedupe key the publisher stamps (sha256 of corr:status)."""
    return hashlib.sha256(f"{correlation_id}:{status}".encode()).hexdigest()


def summarise_completion(event: dict[str, Any]) -> dict[str, Any]:
    """Pull the fields a completion-topic subscriber acts on."""
    return {
        "event": event.get("event"),
        "event_id": event.get("event_id"),
        "external_correlation_id": event.get("external_correlation_id"),
        "openapi_job_id": event.get("openapi_job_id"),
        "status": event.get("status"),
        "request_id": event.get("request_id"),
        "error_code": event.get("error_code"),
        "result_ref": event.get("result_ref"),
        "result_files": event.get("result_files"),
    }


def plan_downloads(event: dict[str, Any]) -> list[dict[str, str]]:
    """Extract (download_url, filename) targets from a succeeded event.

    Reads ``result_files`` — the list the publisher attaches to a succeeded
    transition, each carrying a ``download_url`` pointing at the dashboard's
    authenticated file-streaming gateway. Returns only entries that actually
    carry a URL (the publisher omits it when it could not resolve the dashboard
    public base). Pure / offline — does no I/O.
    """
    files = event.get("result_files")
    if not isinstance(files, list):
        return []
    out: list[dict[str, str]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        url = str(item.get("download_url") or "").strip()
        if not url:
            continue
        name = str(item.get("name") or item.get("file_id") or "result").split("/")[-1]
        out.append({"download_url": url, "filename": name})
    return out


def acquire_bearer_token() -> str:
    """Resolve a bearer token for the dashboard download endpoint.

    Precedence: ``ELB_BEARER_TOKEN`` env (already-acquired token) → an
    ``az account get-access-token`` for the API app-registration audience in
    ``ELB_API_CLIENT_ID``. Returns ``""`` when neither is available so the
    caller can degrade with a clear message instead of crashing.
    """
    token = (os.environ.get("ELB_BEARER_TOKEN") or "").strip()
    if token:
        return token
    client_id = (os.environ.get("ELB_API_CLIENT_ID") or "").strip()
    if not client_id:
        return ""
    import subprocess

    cmd = [
        "az",
        "account",
        "get-access-token",
        "--resource",
        client_id,
        "--query",
        "accessToken",
        "-o",
        "tsv",
    ]
    try:
        out = subprocess.run(  # noqa: S603 — fixed local az CLI token call
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"token acquisition failed: {type(exc).__name__}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError):
            stderr = (exc.stderr or "").strip()
            if "AADSTS65001" in stderr or "has not consented" in stderr:
                print(
                    "  hint: the API app registration must pre-authorize the Azure CLI "
                    "public client (04b07795-8ddb-461a-bbee-02f9e1bf7b46) for its "
                    "'user_impersonation' scope. scripts/dev/setup-app-registration.sh "
                    "does this automatically; otherwise an admin must grant consent. "
                    "Until then, set ELB_BEARER_TOKEN to a pre-acquired token.",
                    file=sys.stderr,
                )
            elif stderr:
                print(f"  az: {stderr[:300]}", file=sys.stderr)
        return ""
    return out.stdout.strip()


def download_result_files(
    event: dict[str, Any], download_dir: str, token: str
) -> list[dict[str, Any]]:
    """Download every result file referenced by a succeeded event.

    Calls each ``download_url`` with the bearer token (the dashboard ``api``
    sidecar streams the bytes — never a SAS URL) and writes it under
    ``download_dir``. Returns a per-file result record.
    """
    import urllib.error
    import urllib.request

    os.makedirs(download_dir, exist_ok=True)
    results: list[dict[str, Any]] = []
    for target in plan_downloads(event):
        url = target["download_url"]
        dest = os.path.join(download_dir, target["filename"])
        record: dict[str, Any] = {"url": url, "dest": dest}
        request = urllib.request.Request(url)  # noqa: S310 — https dashboard URL
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=120) as resp:  # noqa: S310
                size = 0
                with open(dest, "wb") as fh:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
                        size += len(chunk)
                record["status"] = "ok"
                record["bytes"] = size
        except urllib.error.HTTPError as exc:
            record["status"] = f"http_{exc.code}"
        except (urllib.error.URLError, OSError) as exc:
            record["status"] = f"error_{type(exc).__name__}"
        results.append(record)
        print(json.dumps({"download": record}, default=str))
    return results


def consume_requests(max_messages: int, settle: str) -> dict[str, Any]:
    """Receive request-queue messages, settle each, return drain stats."""
    from azure.identity import DefaultAzureCredential
    from azure.servicebus import ServiceBusClient

    stats = {"received": 0, "completed": 0, "abandoned": 0, "dead_lettered": 0}
    budget = max(1, max_messages)
    with ServiceBusClient(NAMESPACE_FQDN, DefaultAzureCredential()) as client:
        with client.get_queue_receiver(
            REQUEST_QUEUE, max_wait_time=_RECEIVE_MAX_WAIT_SECONDS
        ) as receiver:
            while budget > 0:
                batch = receiver.receive_messages(
                    max_message_count=min(budget, 32),
                    max_wait_time=_RECEIVE_MAX_WAIT_SECONDS,
                )
                if not batch:
                    break
                for message in batch:
                    budget -= 1
                    stats["received"] += 1
                    body = parse_body(message)
                    summary = summarise_request(body)
                    action = settle if settle != "auto" else decide_action(summary)
                    print(json.dumps({"received": summary, "action": action}, default=str))
                    try:
                        if action == "complete":
                            receiver.complete_message(message)
                            stats["completed"] += 1
                        elif action == "dead_letter":
                            receiver.dead_letter_message(message, reason="handler_rejected")
                            stats["dead_lettered"] += 1
                        else:
                            receiver.abandon_message(message)
                            stats["abandoned"] += 1
                    except Exception as exc:
                        # Lock lost / expired — broker redelivers. Never raise so
                        # one bad settle does not abort the batch.
                        print(
                            f"settle failed ({action}): {type(exc).__name__}",
                            file=sys.stderr,
                        )
                        stats["abandoned"] += 1
    return stats


def consume_completions(
    subscription: str, max_messages: int, kind: str = COMPLETION_KIND
) -> dict[str, Any]:
    """Consume completion events, dedupe on event_id, settle each.

    ``kind="topic"`` reads a dedicated ``subscription`` on the completion topic
    (fan-out — this consumer gets its own copy); ``kind="queue"`` reads the
    completion entity as a queue (point-to-point — a single competing consumer,
    ``subscription`` ignored).
    """
    from azure.identity import DefaultAzureCredential
    from azure.servicebus import ServiceBusClient

    stats = {"received": 0, "processed": 0, "duplicates": 0}
    seen: set[str] = set()
    budget = max(1, max_messages)
    with ServiceBusClient(NAMESPACE_FQDN, DefaultAzureCredential()) as client:
        if kind == "queue":
            receiver_cm = client.get_queue_receiver(
                COMPLETION_TOPIC, max_wait_time=_RECEIVE_MAX_WAIT_SECONDS
            )
        else:
            receiver_cm = client.get_subscription_receiver(
                COMPLETION_TOPIC, subscription, max_wait_time=_RECEIVE_MAX_WAIT_SECONDS
            )
        with receiver_cm as receiver:
            while budget > 0:
                batch = receiver.receive_messages(
                    max_message_count=min(budget, 32),
                    max_wait_time=_RECEIVE_MAX_WAIT_SECONDS,
                )
                if not batch:
                    break
                for message in batch:
                    budget -= 1
                    stats["received"] += 1
                    event = parse_body(message)
                    summary = summarise_completion(event)
                    event_id = str(summary.get("event_id") or "")
                    if event_id and event_id in seen:
                        stats["duplicates"] += 1
                        print(json.dumps({"duplicate": summary}, default=str))
                    else:
                        if event_id:
                            seen.add(event_id)
                        stats["processed"] += 1
                        print(json.dumps({"event": summary}, default=str))
                    # An observer always completes — it only reads transitions.
                    try:
                        receiver.complete_message(message)
                    except Exception as exc:
                        print(f"complete failed: {type(exc).__name__}", file=sys.stderr)
    return stats


def consume_completions_and_download(
    subscription: str, max_messages: int, download_dir: str, kind: str = COMPLETION_KIND
) -> dict[str, Any]:
    """Subscribe to the completion topic and download results on success.

    Extends :func:`consume_completions` with the end-to-end behaviour the user
    cares about: when a ``succeeded`` event carries ``result_files`` with
    ``download_url`` links, call each link with a bearer token and save the
    bytes locally. Dedupes on ``event_id`` so an at-least-once redelivery does
    not download twice.
    """
    from azure.identity import DefaultAzureCredential
    from azure.servicebus import ServiceBusClient

    token = acquire_bearer_token()
    if not token:
        print(
            "no bearer token (set ELB_BEARER_TOKEN or ELB_API_CLIENT_ID) — "
            "downloads will be attempted unauthenticated and likely 401",
            file=sys.stderr,
        )
    stats = {"received": 0, "processed": 0, "duplicates": 0, "downloaded": 0}
    seen: set[str] = set()
    budget = max(1, max_messages)
    with ServiceBusClient(NAMESPACE_FQDN, DefaultAzureCredential()) as client:
        if kind == "queue":
            receiver_cm = client.get_queue_receiver(
                COMPLETION_TOPIC, max_wait_time=_RECEIVE_MAX_WAIT_SECONDS
            )
        else:
            receiver_cm = client.get_subscription_receiver(
                COMPLETION_TOPIC, subscription, max_wait_time=_RECEIVE_MAX_WAIT_SECONDS
            )
        with receiver_cm as receiver:
            while budget > 0:
                batch = receiver.receive_messages(
                    max_message_count=min(budget, 32),
                    max_wait_time=_RECEIVE_MAX_WAIT_SECONDS,
                )
                if not batch:
                    break
                for message in batch:
                    budget -= 1
                    stats["received"] += 1
                    event = parse_body(message)
                    summary = summarise_completion(event)
                    event_id = str(summary.get("event_id") or "")
                    if event_id and event_id in seen:
                        stats["duplicates"] += 1
                        print(json.dumps({"duplicate": summary}, default=str))
                    else:
                        if event_id:
                            seen.add(event_id)
                        stats["processed"] += 1
                        print(json.dumps({"event": summary}, default=str))
                        if summary.get("status") == "succeeded":
                            downloaded = download_result_files(event, download_dir, token)
                            stats["downloaded"] += sum(
                                1 for d in downloaded if d.get("status") == "ok"
                            )
                    try:
                        receiver.complete_message(message)
                    except Exception as exc:
                        print(f"complete failed: {type(exc).__name__}", file=sys.stderr)
    return stats


def _self_test() -> int:
    """Offline check of parse / routing / dedupe — no Azure, no network."""

    class _ReqMsg:
        body = (
            b'{"program":"blastn","db":"core_nt",'
            b'"external_correlation_id":"corr-1","request_id":"r1",'
            b'"options":{"outfmt":5,"word_size":28}}',
        )

    req_body = parse_body(_ReqMsg())
    summary = summarise_request(req_body)
    assert summary["correlation_id"] == "corr-1"
    assert summary["request_id"] == "r1"
    assert summary["program"] == "blastn"
    assert summary["route"] == "external_submit"
    assert summary["outfmt"] == 5
    assert decide_action(summary) == "complete"

    # v1 (multi-token tabular) routing.
    class _V1Msg:
        body = (
            b'{"program":"blastn","db":"core_nt","external_correlation_id":"corr-2",'
            b'"blast_options":{"outfmt":"7 std staxids sstrand qseq sseq"}}',
        )

    v1_summary = summarise_request(parse_body(_V1Msg()))
    assert v1_summary["route"] == "v1_jobs"
    assert v1_summary["outfmt"] == "7 std staxids sstrand qseq sseq"

    # A body without a correlation id is dead-lettered, never retried forever.
    assert decide_action(summarise_request({"program": "blastn"})) == "dead_letter"

    # Malformed body degrades to {} rather than raising.
    class _BadMsg:
        body = (b"not json",)

    assert parse_body(_BadMsg()) == {}

    # Completion event parse + dedupe key.
    class _EvtMsg:
        body = (
            json.dumps(
                {
                    "event": "blast.transition",
                    "event_id": _event_id("corr-1", "succeeded"),
                    "attempt": 1,
                    "external_correlation_id": "corr-1",
                    "openapi_job_id": "job-abc",
                    "status": "succeeded",
                    "ts": "2026-06-17T00:00:00+00:00",
                    "result_ref": {
                        "api": "GET /api/v1/elastic-blast/jobs/job-abc",
                        "files": "GET /api/v1/elastic-blast/jobs/job-abc/files/{file_id}",
                    },
                    "result_files": [
                        {
                            "file_id": "merged_results.out.gz",
                            "name": "merged_results.out.gz",
                            "format": "blast_tabular",
                            "size": 12345,
                            "download_url": (
                                "https://ca-elb-dashboard.example.com"
                                "/api/v1/elastic-blast/jobs/job-abc/files/merged_results.out.gz"
                            ),
                        }
                    ],
                    "request_id": "r1",
                }
            ).encode(),
        )

    parsed_evt = parse_body(_EvtMsg())
    evt = summarise_completion(parsed_evt)
    assert evt["status"] == "succeeded"
    assert evt["openapi_job_id"] == "job-abc"
    assert evt["event_id"] == _event_id("corr-1", "succeeded")
    assert evt["request_id"] == "r1"
    # The dedupe key is reproducible from (corr, status) alone.
    assert _event_id("corr-1", "succeeded") == _event_id("corr-1", "succeeded")
    assert _event_id("corr-1", "succeeded") != _event_id("corr-1", "running")

    # The consumer turns result_files download_url links into download targets.
    targets = plan_downloads(parsed_evt)
    assert len(targets) == 1
    assert targets[0]["filename"] == "merged_results.out.gz"
    assert targets[0]["download_url"].endswith(
        "/api/v1/elastic-blast/jobs/job-abc/files/merged_results.out.gz"
    )
    assert "blob.core.windows.net" not in targets[0]["download_url"]
    # An entry without a download_url is skipped (publisher could not resolve base).
    assert plan_downloads({"result_files": [{"file_id": "x"}]}) == []

    print("self-test OK: request parse/route/settle + completion parse/dedupe + download plan")
    print(json.dumps({"request": summary, "v1": v1_summary, "completion": evt}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        choices=("requests", "completions"),
        default="requests",
        help="requests = drain the request queue; completions = subscribe to the topic.",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=10,
        help="Bound on how many messages to receive this run.",
    )
    parser.add_argument(
        "--settle",
        choices=("auto", "complete", "abandon", "dead_letter"),
        default="auto",
        help=(
            "requests source only: how to settle each message. 'auto' mirrors "
            "the live drain (complete, or dead-letter a message with no "
            "correlation id). 'abandon' leaves messages on the queue."
        ),
    )
    parser.add_argument(
        "--subscription",
        default=COMPLETION_SUBSCRIPTION,
        help="completions source only: the topic subscription name to read.",
    )
    parser.add_argument(
        "--completion-kind",
        dest="completion_kind",
        choices=("topic", "queue"),
        default=COMPLETION_KIND,
        help=(
            "completions source only: read the completion entity as a topic "
            "subscription (fan-out) or a queue (point-to-point). Defaults to "
            "SERVICEBUS_COMPLETION_KIND or 'topic'."
        ),
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help=(
            "completions source only: on a succeeded event, call each "
            "result_files download_url with a bearer token and save the bytes."
        ),
    )
    parser.add_argument(
        "--download-dir",
        dest="download_dir",
        default="./downloads",
        help="completions + --download: directory to write result files into.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Validate parse/route/dedupe offline (no Azure).",
    )
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    print(f"namespace : {NAMESPACE_FQDN}")
    try:
        if args.source == "requests":
            print(f"queue     : {REQUEST_QUEUE}")
            stats = consume_requests(args.max, args.settle)
        else:
            if args.completion_kind == "queue":
                print(f"queue     : {COMPLETION_TOPIC} (completion, point-to-point)")
            else:
                print(f"topic     : {COMPLETION_TOPIC} / {args.subscription}")
            if args.download:
                print(f"download  : {args.download_dir}")
                stats = consume_completions_and_download(
                    args.subscription, args.max, args.download_dir, args.completion_kind
                )
            else:
                stats = consume_completions(args.subscription, args.max, args.completion_kind)
    except Exception as exc:
        print(f"\nreceive failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"\nstats: {json.dumps(stats)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
