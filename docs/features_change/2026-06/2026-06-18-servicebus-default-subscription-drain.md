# Drain the shared `default` completion subscription (multi-subscription observer)

## Motivation

The completion topic `elastic-blast-completions` is fan-out: every published
`blast.transition` event is copied to every subscription. The in-deployment
observer only drained its dedicated subscription (`playground-observer`), so the
shared `default` subscription — which a real external integrator would otherwise
own — had no consumer and piled up unread (observed: 15 active messages on
`default`, none consumed). Those events are completion notifications of jobs that
already ran (status `queued`/`running`/`succeeded`), not unexecuted requests, but
they still accumulate forever without a receiver.

Request: drain `default` too, and label each observed event so `default` can be
told apart from any other subscription.

## User-facing change

* The optional external-completion observer now drains **multiple**
  subscriptions in one round-robin loop (default `playground-observer` **and**
  `default`), so `default` no longer accumulates unread completion events.
* `SERVICEBUS_COMPLETION_SUBSCRIPTION` is now a **comma-separated** list (blank
  entries dropped, order-preserving de-dup). A single value still works.
* Each observed completion is tagged with the **subscription** it came from. The
  Service Bus Playground renders a per-event subscription badge and lists every
  subscription the observer drains.
* A subscription that does not exist is logged once and skipped for the rest of
  the process (no hot-loop); when every configured subscription is permanently
  gone the loop exits instead of spinning.

> The observer remains gated default-OFF behind `SERVICEBUS_EXTERNAL_CONSUMER`
> (charter §12a Rule 4). To actually drain `default` in a deployment, enable that
> gate on the worker sidecar and redeploy; the code change alone does not start
> the loop.

## API / behaviour diff

* `api/services/service_bus_external_consumer.py`
  * new `completion_subscriptions() -> list[str]` (comma-separated env parse);
    `completion_subscription()` kept, returns the primary (first) one.
  * `consume_completions(...)` gains `subscriptions: list[str] | None`,
    round-robins live subscriptions each tick, retires a
    `MessagingEntityNotFoundError` subscription via a skip-set, and exits when
    all are gone. `on_event` signature is now `(event, subscription)`.
* `api/services/service_bus_completions.py`
  * `record_completion(event, *, subscription=None)` stores a `subscription`
    field; `list_recent` de-dup key is now `(event_id, subscription)` so the same
    fan-out event on different subscriptions both survive (each source visible),
    while a same-`(event_id, subscription)` redelivery is still de-duped.
* `api/routes/settings/service_bus.py`
  * `GET /observed-completions` adds a `subscriptions` list; `subscription`
    (primary) is kept for backward compatibility.
* `web/src/api/settings.ts` + `web/src/pages/ServiceBusPlayground.tsx`
  * `ServiceBusObservedCompletion.subscription?`,
    `ServiceBusObservedCompletionsResponse.subscriptions?`; per-event
    subscription badge and multi-subscription "observed on" label; React key now
    includes the subscription.

No new dependency, no RBAC change, no new auth guard, no SSE change, Storage
network posture unchanged.

## Validation

* `uv run ruff check` on all changed Python files — clean.
* `uv run pytest -q -n auto api/tests/ -k "service_bus or completion or playground"`
  — 123 passed (includes new multi-subscription / skip-set / dedup coverage and
  the persona matrix).
* `cd web && npm run build` — type-checks and builds clean.
</content>
</invoke>
