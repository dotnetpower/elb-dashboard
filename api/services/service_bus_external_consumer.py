"""Optional external-completion consumer — subscribe to the completion topic.

A long-running loop that subscribes to the Service Bus completion topic
(``SERVICEBUS_RESPONSE_TOPIC``) on one or more subscriptions and records each
``blast.transition`` event it receives, tagged with the subscription it came
from. It models the *external* subscriber an integrating service would run on
its own infrastructure: the dashboard publishes one transition per job status
change to the topic, and any number of subscriptions fan that event out to
independent consumers. The in-deployment observer drains the dedicated demo
subscription AND the shared ``default`` subscription (comma-separated
``SERVICEBUS_COMPLETION_SUBSCRIPTION`` override) so ``default`` does not pile up
unread, and each observation is labelled so they can be told apart.

When the completion entity is a **queue** (``SERVICEBUS_COMPLETION_KIND=queue``)
the model is point-to-point instead: there is no fan-out, so a single consumer
competes for every message. In that mode the in-deployment demo observer is
intentionally NOT started (it would steal messages from the real external
consumer); only the standalone entry point reads the queue.

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
Key entry points: ``consume_completions``, ``completion_subscriptions``,
    ``external_consumer_enabled``, ``start_external_consumer``,
    ``stop_external_consumer``, ``EXTERNAL_CONSUMER_ENV``.
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
# The completion topic is fan-out: every subscription gets its OWN copy of each
# event. The in-deployment observer drains MULTIPLE subscriptions so both the
# dedicated demo subscription AND the shared "default" subscription (which a real
# external integrator would otherwise own, and which otherwise piles up unread)
# are consumed. Each observed event is tagged with the subscription it came from
# so the Playground can tell "default" apart from any other subscription.
DEFAULT_SUBSCRIPTIONS: tuple[str, ...] = (DEFAULT_SUBSCRIPTION, "default")

_POLL_WAIT_SECONDS = max(1, int(os.environ.get("SERVICEBUS_EXTERNAL_POLL_SECONDS", "5")))
_RECEIVE_BATCH = max(1, int(os.environ.get("SERVICEBUS_EXTERNAL_BATCH", "16")))
_BACKOFF_START_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 30.0

_thread: threading.Thread | None = None
_stop_event: threading.Event | None = None
_thread_lock = threading.Lock()


def completion_subscriptions() -> list[str]:
    """The completion-topic subscriptions the observer drains (env override).

    ``SERVICEBUS_COMPLETION_SUBSCRIPTION`` is a comma-separated list; blank
    entries are dropped and order-preserving de-duplication is applied. Falls
    back to ``DEFAULT_SUBSCRIPTIONS`` when unset/blank so the shared ``default``
    subscription is always drained alongside the dedicated demo one.
    """
    raw = os.environ.get(SUBSCRIPTION_ENV, "")
    names = [p.strip() for p in raw.split(",") if p.strip()]
    if not names:
        return list(DEFAULT_SUBSCRIPTIONS)
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def completion_subscription() -> str:
    """The primary (first) completion-topic subscription name.

    Kept for backward compatibility with callers/UI that show a single
    subscription label; the observer itself drains ``completion_subscriptions()``.
    """
    subs = completion_subscriptions()
    return subs[0] if subs else DEFAULT_SUBSCRIPTION


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
    on_event: Callable[[dict[str, Any], str], None],
    subscription: str | None = None,
    subscriptions: list[str] | None = None,
    credential: Any | None = None,
    connection_string: str | None = None,
    stop: threading.Event | None = None,
    max_wait_seconds: int = _POLL_WAIT_SECONDS,
    receive_batch: int = _RECEIVE_BATCH,
    max_iterations: int | None = None,
    kind: str = "topic",
) -> int:
    """Receive completion events from topic subscription(s) (or a queue) until stopped.

    Calls ``on_event(parsed_json_body, subscription_label)`` for each received
    message, then completes it. The ``subscription_label`` tells the sink which
    subscription the event came from (the queue name in ``queue`` mode) so a
    consumer can tell ``default`` apart from any other subscription. Returns the
    number of events delivered to ``on_event``. The loop is bounded
    (``max_iterations`` for tests), interruptible (``stop``), and backs off on
    error instead of hot-looping. Auth: pass ``credential`` (Entra) or
    ``connection_string`` (SAS); exactly one is used.

    Pass either a single ``subscription`` or a list of ``subscriptions``; the
    loop round-robins every live subscription each tick (topic fan-out delivers
    one copy per subscription). A subscription that does not exist raises a
    permanent ``MessagingEntityNotFoundError`` — it is logged once and dropped
    for the rest of this process (retrying it every tick would be pointless and
    spam the log); the other subscriptions keep draining. When every configured
    subscription is permanently gone the loop exits.

    ``kind`` selects the completion entity model: ``"topic"`` (default) reads the
    subscription(s) on the topic (fan-out — this consumer gets its own copy);
    ``"queue"`` reads ``topic`` as a queue name (point-to-point — this consumer
    COMPETES with any other consumer of the same queue, and the subscription
    arguments are ignored).

    Imported lazily so the module stays cheap to import in the api sidecar (the
    SDK is only needed where the loop actually runs).
    """
    from azure.servicebus import ServiceBusClient
    from azure.servicebus.exceptions import MessagingEntityNotFoundError, ServiceBusError

    is_queue = str(kind).strip().lower() == "queue"
    if not namespace_fqdn or not topic:
        raise ValueError("namespace_fqdn and entity name are required")
    subs = list(subscriptions) if subscriptions else ([subscription] if subscription else [])
    if not is_queue and not subs:
        raise ValueError("at least one subscription is required for topic completion entities")

    delivered = 0
    backoff = _BACKOFF_START_SECONDS
    iterations = 0
    # Subscriptions that returned a permanent "entity not found". Retrying them
    # every tick is pointless and would spam the log, so drop them for the rest
    # of this process's lifetime (a redeploy re-reads the config).
    dead_subscriptions: set[str] = set()

    def _client() -> ServiceBusClient:
        if connection_string:
            return ServiceBusClient.from_connection_string(connection_string)
        cred = credential
        if cred is None:
            from api.services import get_credential

            cred = get_credential()
        return ServiceBusClient(namespace_fqdn, cred)

    def _drain_one(client: ServiceBusClient, sub_name: str | None) -> bool:
        """Drain one subscription (or the queue). Returns True if it progressed.

        Raises ``MessagingEntityNotFoundError`` for a permanently-missing
        subscription so the caller can retire it; other ``ServiceBusError`` are
        re-raised for the tick-level backoff.
        """
        nonlocal delivered
        if is_queue:
            receiver_cm = client.get_queue_receiver(
                queue_name=topic, max_wait_time=max_wait_seconds
            )
        else:
            receiver_cm = client.get_subscription_receiver(
                topic_name=topic,
                subscription_name=sub_name,
                max_wait_time=max_wait_seconds,
            )
        progressed = False
        label = sub_name if sub_name is not None else topic
        with receiver_cm as receiver:
            batch = receiver.receive_messages(
                max_message_count=receive_batch, max_wait_time=max_wait_seconds
            )
            for message in batch:
                event = _parse_body(message)
                try:
                    on_event(event, label)
                    delivered += 1
                    progressed = True
                except Exception:
                    LOGGER.exception("external consumer on_event raised; abandoning")
                    _safe_abandon(receiver, message)
                    continue
                _safe_complete(receiver, message)
        return progressed

    while stop is None or not stop.is_set():
        if max_iterations is not None and iterations >= max_iterations:
            break
        iterations += 1
        progressed = False
        had_error = False
        try:
            targets: list[str | None] = (
                [None] if is_queue else [s for s in subs if s not in dead_subscriptions]
            )
            with _client() as client:
                for sub_name in targets:
                    if stop is not None and stop.is_set():
                        break
                    try:
                        if _drain_one(client, sub_name):
                            progressed = True
                    except MessagingEntityNotFoundError:
                        if not is_queue and sub_name is not None:
                            dead_subscriptions.add(sub_name)
                            LOGGER.warning(
                                "external consumer: completion subscription %r not found on "
                                "topic %r; skipping it for the rest of this process (create it "
                                "or fix SERVICEBUS_COMPLETION_SUBSCRIPTION)",
                                sub_name,
                                topic,
                            )
                        else:
                            raise
                    except ServiceBusError as exc:
                        had_error = True
                        LOGGER.warning(
                            "external consumer receive failed (entity=%s): %s",
                            sub_name if not is_queue else topic,
                            type(exc).__name__,
                        )
            # Every configured subscription is permanently gone — nothing left to do.
            if not is_queue and subs and all(s in dead_subscriptions for s in subs):
                LOGGER.warning(
                    "external consumer: no live completion subscriptions remain; stopping loop"
                )
                break
            if had_error and not progressed:
                if _wait(stop, backoff):
                    break
                backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)
            else:
                backoff = _BACKOFF_START_SECONDS  # reset after a clean iteration
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


def _record_to_observer(event: dict[str, Any], subscription: str) -> None:
    """Demo sink: record an observed completion (tagged with its source
    subscription) + log it. Best-effort."""
    try:
        from api.services.service_bus_completions import record_completion

        record_completion(event, subscription=subscription)
    except Exception:  # pragma: no cover - best-effort
        LOGGER.debug("external consumer observer record failed", exc_info=True)
    LOGGER.info(
        "external consumer observed completion sub=%s corr=%s status=%s job=%s",
        subscription,
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
    kind = getattr(cfg, "completion_kind", "topic")
    if not cfg.namespace_fqdn or not topic:
        LOGGER.info("external consumer: namespace/completion entity not configured; not starting")
        return 0
    if str(kind).strip().lower() == "queue":
        # The completion entity is a point-to-point queue. The in-deployment demo
        # observer must NOT drain it, or it would steal messages from the real
        # external consumer (queues have no fan-out). The standalone entry point
        # (`python -m api.services.service_bus_external_consumer`) is the queue
        # consumer an external party runs on its own infrastructure.
        LOGGER.warning(
            "external consumer: completion entity is a queue (point-to-point); the "
            "in-deployment demo observer is disabled so it cannot compete with the "
            "real external consumer for messages"
        )
        return 0
    return consume_completions(
        namespace_fqdn=cfg.namespace_fqdn,
        topic=topic,
        subscriptions=completion_subscriptions(),
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
        LOGGER.info(
            "external completion consumer started (subs=%s)", completion_subscriptions()
        )
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

    Reads ``SERVICEBUS_NAMESPACE_FQDN``, ``SERVICEBUS_RESPONSE_TOPIC``,
    ``SERVICEBUS_COMPLETION_SUBSCRIPTION`` and ``SERVICEBUS_COMPLETION_KIND``
    from the environment and authenticates with ``DefaultAzureCredential``
    (Entra). An external party copies this file and runs
    ``python service_bus_external_consumer.py``. With
    ``SERVICEBUS_COMPLETION_KIND=queue`` the completion entity is read as a queue
    (point-to-point) and the subscription env is ignored.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    namespace = os.environ.get("SERVICEBUS_NAMESPACE_FQDN", "").strip()
    topic = os.environ.get("SERVICEBUS_RESPONSE_TOPIC", "elastic-blast-completions").strip()
    subscriptions = completion_subscriptions()
    kind = os.environ.get("SERVICEBUS_COMPLETION_KIND", "").strip().lower() or "topic"
    if kind not in {"topic", "queue"}:
        kind = "topic"
    if not namespace:
        print("SERVICEBUS_NAMESPACE_FQDN is required (e.g. <ns>.servicebus.windows.net)")
        return 2
    from azure.identity import DefaultAzureCredential

    def _print(event: dict[str, Any], subscription: str) -> None:
        print(json.dumps({"_subscription": subscription, **event}, default=str))

    stop = threading.Event()
    try:
        consume_completions(
            namespace_fqdn=namespace,
            topic=topic,
            subscriptions=subscriptions,
            on_event=_print,
            credential=DefaultAzureCredential(),
            stop=stop,
            kind=kind,
        )
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":  # pragma: no cover - manual / external run
    raise SystemExit(_standalone_main())
