"""Optional external-completion consumer — subscribe to the completion topic.

A long-running loop that subscribes to the Service Bus completion topic
(``SERVICEBUS_RESPONSE_TOPIC``) on a dedicated subscription and records each
``blast.transition`` event it receives. It models the *external* subscriber an
integrating service would run on its own infrastructure: the dashboard publishes
one transition per job status change to the topic, and any number of
subscriptions fan that event out to independent consumers.

Two ways to run it:

* **In-deployment demo (gated, default-OFF).** When ``SERVICEBUS_EXTERNAL_CONSUMER``
  is enabled, the worker sidecar starts one daemon loop (see
  ``api/celery_signals.py``) that records observations into the shared Redis ring
  (``service_bus_completions``) so the Playground can show them. It uses the
  shared managed identity and is purely observational — it NEVER executes BLAST,
  so it can never double-run a job (the request-queue consumer is the sole
  executor). This is a demonstration aid, not a third party.
* **Standalone reference (external party).** ``python -m
  api.services.service_bus_external_consumer`` runs the same loop printing each
  event, authenticating with ``DefaultAzureCredential``. An external service
  copies this file (only ``azure-servicebus`` + ``azure-identity`` needed) and
  points it at its own subscription with ``Azure Service Bus Data Receiver``.

Responsibility: Own the completion-subscription receive loop lifecycle
    (bounded, interruptible, backoff) plus the gated worker launcher and the
    standalone entry point. No request-queue draining, no BLAST execution.
Edit boundaries: Service Bus receive only. Recording observations is delegated
    to ``service_bus_completions``; per-message business logic does not belong
    here. Keep the loop bounded and never raise out of the loop body.
Key entry points: ``consume_completions``, ``external_consumer_enabled``,
    ``start_external_consumer``, ``stop_external_consumer``,
    ``EXTERNAL_CONSUMER_ENV``.
Risky contracts: ``consume_completions`` MUST exit promptly when the stop event
    is set, MUST back off (capped) after an error instead of hot-looping, and
    MUST settle (complete) each received message. The worker launcher reuses the
    same single-daemon guard as the resident request-queue consumer so only the
    main worker process runs it.
Validation: ``uv run pytest -q api/tests/test_service_bus_external_consumer.py``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from typing import Any

LOGGER = logging.getLogger(__name__)

# Default-OFF gate (charter §12a Rule 4). When unset/false the loop never starts.
EXTERNAL_CONSUMER_ENV = "SERVICEBUS_EXTERNAL_CONSUMER"
# Dedicated subscription on the completion topic for this consumer. A dedicated
# subscription (NOT the shared "default") means the demo consumer receives its
# OWN copy of every completion and never competes with a real external
# subscriber for messages (topic fan-out = one copy per subscription).
SUBSCRIPTION_ENV = "SERVICEBUS_COMPLETION_SUBSCRIPTION"
DEFAULT_SUBSCRIPTION = "playground-observer"

_POLL_WAIT_SECONDS = max(1, int(os.environ.get("SERVICEBUS_EXTERNAL_POLL_SECONDS", "5")))
_RECEIVE_BATCH = max(1, int(os.environ.get("SERVICEBUS_EXTERNAL_BATCH", "16")))
_BACKOFF_START_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 30.0

_thread: threading.Thread | None = None
_stop_event: threading.Event | None = None
_thread_lock = threading.Lock()


def completion_subscription() -> str:
    """The dedicated completion-topic subscription name (env override)."""
    return (os.environ.get(SUBSCRIPTION_ENV, "").strip() or DEFAULT_SUBSCRIPTION)


def external_consumer_enabled() -> bool:
    """True when the demo external consumer should run (gate env AND SB enabled)."""
    if os.environ.get(EXTERNAL_CONSUMER_ENV, "").strip().lower() not in {"1", "true", "yes"}:
        return False
    try:
        from api.services.service_bus_pref import service_bus_enabled

        return bool(service_bus_enabled())
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.debug("external consumer gate check failed: %s", type(exc).__name__)
        return False


def consume_completions(
    *,
    namespace_fqdn: str,
    topic: str,
    subscription: str,
    on_event: Callable[[dict[str, Any]], None],
    credential: Any | None = None,
    connection_string: str | None = None,
    stop: threading.Event | None = None,
    max_wait_seconds: int = _POLL_WAIT_SECONDS,
    receive_batch: int = _RECEIVE_BATCH,
    max_iterations: int | None = None,
) -> int:
    """Receive completion events from a topic subscription until stopped.

    Calls ``on_event(parsed_json_body)`` for each received message, then
    completes it. Returns the number of events delivered to ``on_event``. The
    loop is bounded (``max_iterations`` for tests), interruptible (``stop``),
    and backs off on error instead of hot-looping. Auth: pass ``credential``
    (Entra) or ``connection_string`` (SAS); exactly one is used.

    Imported lazily so the module stays cheap to import in the api sidecar (the
    SDK is only needed where the loop actually runs).
    """
    from azure.servicebus import ServiceBusClient
    from azure.servicebus.exceptions import ServiceBusError

    if not namespace_fqdn or not topic or not subscription:
        raise ValueError("namespace_fqdn, topic and subscription are required")

    delivered = 0
    backoff = _BACKOFF_START_SECONDS
    iterations = 0

    def _client() -> ServiceBusClient:
        if connection_string:
            return ServiceBusClient.from_connection_string(connection_string)
        cred = credential
        if cred is None:
            from api.services import get_credential

            cred = get_credential()
        return ServiceBusClient(namespace_fqdn, cred)

    while stop is None or not stop.is_set():
        if max_iterations is not None and iterations >= max_iterations:
            break
        iterations += 1
        try:
            with _client() as client, client.get_subscription_receiver(
                topic_name=topic,
                subscription_name=subscription,
                max_wait_time=max_wait_seconds,
            ) as receiver:
                batch = receiver.receive_messages(
                    max_message_count=receive_batch,
                    max_wait_time=max_wait_seconds,
                )
                for message in batch:
                    event = _parse_body(message)
                    try:
                        on_event(event)
                        delivered += 1
                    except Exception:
                        LOGGER.exception("external consumer on_event raised; abandoning")
                        _safe_abandon(receiver, message)
                        continue
                    _safe_complete(receiver, message)
            backoff = _BACKOFF_START_SECONDS  # reset after a clean iteration
        except ServiceBusError as exc:
            LOGGER.warning(
                "external consumer receive failed (sub=%s): %s; backing off %.0fs",
                subscription,
                type(exc).__name__,
                backoff,
            )
            if _wait(stop, backoff):
                break
            backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)
        except Exception as exc:  # never let the loop die silently
            LOGGER.exception("external consumer loop error: %s", type(exc).__name__)
            if _wait(stop, backoff):
                break
            backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)
    return delivered


def _parse_body(message: Any) -> dict[str, Any]:
    """Best-effort parse a Service Bus message body into a JSON dict.

    The SDK exposes ``message.body`` as ``bytes``/``str`` or a generator of byte
    chunks (generator-backed AMQP payloads). Normalise both to text, then JSON.
    A non-dict or unparseable body degrades to an empty dict rather than raising.
    """
    body = getattr(message, "body", None)
    try:
        if isinstance(body, bytes | bytearray):
            raw: str = bytes(body).decode("utf-8", "replace")
        elif isinstance(body, str):
            raw = body
        elif body is None:
            raw = ""
        else:
            # Generator / iterable of byte (or str) chunks.
            chunks = [c if isinstance(c, bytes | bytearray) else str(c).encode() for c in body]
            raw = b"".join(chunks).decode("utf-8", "replace")
        parsed = json.loads(raw) if raw else {}
        return parsed if isinstance(parsed, dict) else {"raw": parsed}
    except Exception:
        return {}


def _safe_complete(receiver: Any, message: Any) -> None:
    try:
        receiver.complete_message(message)
    except Exception:
        LOGGER.debug("external consumer complete failed (lock lost?)", exc_info=True)


def _safe_abandon(receiver: Any, message: Any) -> None:
    try:
        receiver.abandon_message(message)
    except Exception:
        LOGGER.debug("external consumer abandon failed (lock lost?)", exc_info=True)


def _wait(stop: threading.Event | None, seconds: float) -> bool:
    """Sleep up to ``seconds``; return True if a stop was requested."""
    if stop is None:
        import time

        time.sleep(seconds)
        return False
    return stop.wait(seconds)


# --------------------------------------------------------------------------- #
# Worker daemon launcher (mirrors resident_consumer's single-daemon guard)
# --------------------------------------------------------------------------- #


def _record_to_observer(event: dict[str, Any]) -> None:
    """Demo sink: record an observed completion + log it. Best-effort."""
    try:
        from api.services.service_bus_completions import record_completion

        record_completion(event)
    except Exception:  # pragma: no cover - best-effort
        LOGGER.debug("external consumer observer record failed", exc_info=True)
    LOGGER.info(
        "external consumer observed completion corr=%s status=%s job=%s",
        event.get("external_correlation_id"),
        event.get("status"),
        event.get("openapi_job_id"),
    )


def run_external_consumer(stop: threading.Event, **overrides: Any) -> int:
    """Run the demo consumer loop against the configured completion topic.

    Resolves namespace/topic/subscription from the saved config + env. Records
    each event into the observer ring. Returns events delivered. Test seams are
    passed through ``overrides`` (e.g. ``max_iterations``).
    """
    from api.services.service_bus_pref import get_service_bus_config

    cfg = get_service_bus_config()
    topic = cfg.completion_topic
    if not cfg.namespace_fqdn or not topic:
        LOGGER.info("external consumer: namespace/topic not configured; not starting")
        return 0
    return consume_completions(
        namespace_fqdn=cfg.namespace_fqdn,
        topic=topic,
        subscription=completion_subscription(),
        on_event=_record_to_observer,
        stop=stop,
        **overrides,
    )


def start_external_consumer() -> bool:
    """Start the worker-side demo consumer daemon. Returns True if started.

    No-op (returns False) when the gate is off or a loop is already running.
    Mirrors ``resident_consumer.start_resident_consumer`` so only the main
    worker process runs a single loop.
    """
    global _thread, _stop_event
    if not external_consumer_enabled():
        return False
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return False
        stop = threading.Event()

        def _loop() -> None:
            try:
                run_external_consumer(stop)
            except Exception:  # pragma: no cover - defensive
                LOGGER.exception("external consumer daemon crashed")

        thread = threading.Thread(
            target=_loop, name="sb-external-consumer", daemon=True
        )
        _stop_event = stop
        _thread = thread
        thread.start()
        LOGGER.info("external completion consumer started (sub=%s)", completion_subscription())
        return True


def stop_external_consumer(timeout: float = 5.0) -> None:
    """Signal the daemon loop to stop and join it (best-effort)."""
    global _thread, _stop_event
    with _thread_lock:
        stop = _stop_event
        thread = _thread
        _thread = None
        _stop_event = None
    if stop is not None:
        stop.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)


def reset_external_consumer_state_for_test() -> None:
    """Clear module-level daemon state (pytest hook)."""
    global _thread, _stop_event
    with _thread_lock:
        _thread = None
        _stop_event = None


def _standalone_main() -> int:
    """Standalone entry point for an external subscriber (prints each event).

    Reads ``SERVICEBUS_NAMESPACE_FQDN``, ``SERVICEBUS_RESPONSE_TOPIC`` and
    ``SERVICEBUS_COMPLETION_SUBSCRIPTION`` from the environment and authenticates
    with ``DefaultAzureCredential`` (Entra). An external party copies this file
    and runs ``python service_bus_external_consumer.py``.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    namespace = os.environ.get("SERVICEBUS_NAMESPACE_FQDN", "").strip()
    topic = os.environ.get("SERVICEBUS_RESPONSE_TOPIC", "elastic-blast-completions").strip()
    subscription = os.environ.get(SUBSCRIPTION_ENV, "").strip() or DEFAULT_SUBSCRIPTION
    if not namespace:
        print("SERVICEBUS_NAMESPACE_FQDN is required (e.g. <ns>.servicebus.windows.net)")
        return 2
    from azure.identity import DefaultAzureCredential

    def _print(event: dict[str, Any]) -> None:
        print(json.dumps(event, default=str))

    stop = threading.Event()
    try:
        consume_completions(
            namespace_fqdn=namespace,
            topic=topic,
            subscription=subscription,
            on_event=_print,
            credential=DefaultAzureCredential(),
            stop=stop,
        )
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":  # pragma: no cover - manual / external run
    raise SystemExit(_standalone_main())
