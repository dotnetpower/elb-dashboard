---
title: Fix the Service Bus bridge claim/release etag wedge that silently dropped requests
description: Read the Table entity etag from TableEntity.metadata instead of the mapping key so the stale-claim steal and the unconfirmed-reservation release stop failing, which was wedging correlation ids so their BLAST request was never submitted and no completion event was ever published.
tags: [blast, operate]
---

# Fix the Service Bus bridge claim/release etag wedge

## Motivation

A producer reported that requests sent to the Service Bus request queue sometimes
never received a completion event. A production audit of `ca-elb-dashboard`
(Log Analytics workspace `648cd0d4-…`) confirmed a real, narrow defect.

The completion path itself was healthy — terminal events were being published
(241 / 84 / 109 / 26 on 08-02 → 08-05), the durable response outbox had zero
pending rows, and the `elastic-blast-completions` topic's single `default`
subscription (`TrueRuleFilter`) held zero active and zero dead-lettered
messages. The failure was one step earlier, in the drain's single-writer
reservation.

`_claim_table` and `_release_table` read the optimistic-concurrency etag as
`dict(entity).get("odata.etag")`. `azure-data-tables` moves every `odata.*`
field of the wire payload into `TableEntity.metadata` during deserialization, so
that expression is **always `None`**, and the SDK then raises
`ValueError: IfNotModified must be specified with etag.`

The resulting wedge, reproduced exactly in the logs for
`wf3:718:exclusive:E3L:72551461`:

1. `06:15:31Z` — the drain wins the claim, then defers the submit because the
   sibling OpenAPI plane is not ready (`openapi_not_ready`). The rollback
   `release_bridge` raises, is swallowed at `DEBUG`, and the placeholder row
   survives.
2. `06:16:52Z` – `06:18:07Z` — the drain's own `retry-…` redeliveries of the
   *same* request see the leftover reservation and log `claim_contended`
   (message ids confirm a single producer send, `delivery_count=0`; these are
   not producer duplicates).
3. `06:19:45Z` – `06:20:00Z` — past the 180 s stale threshold the steal path
   raises `ValueError` out of `claim_bridge`, so `_safe_drain_handler` abandons
   the message.
4. The request is never submitted: no job, no `queued` ack, no completion event.
   The unconfirmed bridge row lingers as an active bridge (visible as the
   permanent `publish_transitions` floor of `scanned: 14 / published: 0`) until
   the 7-day `bridge_unconfirmed_timeout` finally emits a terminal failure.

Measured blast radius over 7 days: 126 `ValueError` occurrences and 13
correlation ids that hit `claim contended` and never reached `stage=accepted`
(9 real WF3 requests, 4 load-test messages). On 2026-08-05 (KST) 52 of 54
requests completed normally with `completion_published`; the only 2 failures
were this wedge.

## User-facing change

A request whose submit is deferred (most commonly while AKS is stopped or
warming up) is now correctly rolled back and re-submitted on the next
redelivery, instead of being wedged permanently. From the producer's point of
view, the "request accepted but no completion event, ever" case is gone.

## Change summary

* [api/services/service_bus_tracking.py](../../../api/services/service_bus_tracking.py)
  * New `_entity_etag(entity)` helper: reads `metadata["etag"]` first and keeps
    the mapping key only as a fallback for plain-dict callers/fixtures. Returns
    `""` when no etag is available rather than handing `None` to the SDK.
  * `_claim_table` keeps the `TableEntity` (no longer `dict()`s it before
    extracting the etag) and passes the real etag to the conditional steal.
    When no etag is available it now **refuses** the steal (returns `False`,
    logged at `WARNING`) — deferring a delivery is safe, an unguarded steal
    would risk a duplicate BLAST submit.
  * `_release_table` likewise uses the real etag for the conditional delete, and
    skips (with a `WARNING`) when none is available.
  * `_release_table`'s catch-all moved from `DEBUG` to `WARNING`. The
    `DEBUG` level is what hid this failure in production; the release stays
    best-effort by contract but is no longer silent.
  * Module context header gained the etag contract under `Risky contracts`.
* No IaC, route, schema, or frontend change. No new dependency.

Audited every other `MatchConditions.IfNotModified` call site — `auto_stop.py`,
`auto_warmup.py`, `upgrade/state.py`, `storage/prepare_db_metadata.py`, and
`scripts/dev/openapi-overlays/eta.py` all already read the etag correctly.
`service_bus_tracking.py` was the only affected module.

## Validation

* `uv run pytest -q api/tests/test_service_bus_tracking.py` — 18 passed.
  Six new Table-backend tests drive `_claim_table` / `_release_table` through a
  fake `TableClient` that reproduces the SDK's own precondition
  (`raise ValueError("IfNotModified must be specified with etag.")` on a falsy
  etag) against a real `TableEntity` whose etag lives in `metadata`, so the
  pre-fix code fails them:
  * `test_claim_table_steals_stale_reservation_using_metadata_etag`
  * `test_claim_table_refuses_steal_when_etag_is_unavailable`
  * `test_claim_table_does_not_steal_a_confirmed_row`
  * `test_release_table_deletes_unconfirmed_row_using_metadata_etag`
  * `test_release_table_never_deletes_a_confirmed_row`
  * `test_entity_etag_prefers_metadata_over_mapping_key`
* `uv run pytest -q api/tests` — 4953 passed, 3 skipped.
* `uv run ruff check api` — clean.

## Operational notes

* The 9 already-lost requests do **not** self-heal: their messages left the
  queue before the fix. The producer must resend those correlation ids.
* The pre-existing unconfirmed bridge rows stay active until their 7-day
  `bridge_unconfirmed_timeout`, which emits a terminal failure event — late, but
  not silent.
* This module runs in the worker sidecar, so the fix reaches production with the
  next image rollout; there is no template or sidecar-layout change.
