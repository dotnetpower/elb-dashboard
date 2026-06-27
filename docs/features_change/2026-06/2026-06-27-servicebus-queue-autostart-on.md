---
title: SERVICEBUS_QUEUE_AUTOSTART default ON (worker)
description: Activate queue-arrival cluster auto-start so a Service Bus request on a Stopped AKS cluster triggers `az aks start` without operator intervention, pairing with the existing AKS_AUTOSTOP_RESPECT_SB_QUEUE keep-alive.
tags: [release, blast, operate]
---

# Service Bus queue-arrival auto-start ON (worker sidecar)

## Motivation

The SB Tier-B tuning landed `AKS_AUTOSTOP_RESPECT_SB_QUEUE=true` (default-ON
in code) so the auto-stop evaluator refuses to stop a cluster while the
request queue is non-empty. The symmetric **start** side
(`SERVICEBUS_QUEUE_AUTOSTART`) has shipped as default-OFF since the feature
landed (issue #36 Tier 3) so a cold-start would still need an operator to
run `az aks start` manually after the auto-stop window elapsed and the
first burst arrived.

Now that the keep-alive direction is on, the asymmetric pair is the actual
operational pain point: the cluster stops on idle but the next burst sits in
the queue until an operator notices. Activating auto-start finishes the pair
so the queue is the single source of truth for "is this cluster needed".

This change does **not** reduce the cold-start latency itself
(init-ssd / BLAST DB re-download still takes ~30 min after a stop→start);
it only removes the operator-in-the-loop step so the cycle starts the
moment the first SB message lands.

## User-facing change

| File | Change |
|---|---|
| [infra/control-plane-env.json](../../../infra/control-plane-env.json) | worker: new key `SERVICEBUS_QUEUE_AUTOSTART: "true"` |
| [infra/modules/containerAppControl.bicep](../../../infra/modules/containerAppControl.bicep) | worker env: new entry `{ name: 'SERVICEBUS_QUEUE_AUTOSTART', value: controlPlaneEnv.worker.SERVICEBUS_QUEUE_AUTOSTART }` |

No code change — `api/services/aks/queue_autostart.py` already gates on this
env, and both the SB drain (`api/tasks/servicebus/tasks.py::drain_and_resubmit`)
and the idle-autostop evaluator (`api/tasks/azure/idle_autostop.py`) read
`queue_autostart_enabled()` at request time.

## Operational characteristics

- **Trigger**: worker drain tick observes `power_state == "Stopped"` AND
  `pending_request_count > 0` AND `queue_autostart_enabled() is True`.
- **Single-flight**: `acquire_autostart_lease()` takes a Redis lease keyed
  `aks:queue-autostart:<sub>:<rg>:<cluster>` with TTL =
  `SERVICEBUS_QUEUE_AUTOSTART_COOLDOWN_SECONDS` (default 600s, floored 60s).
  Concurrent workers race on the lease; only the winner runs `az aks start`.
- **Cooldown**: the same lease doubles as the post-start cooldown — no second
  start within the TTL even if the cluster is intermittently Stopped again.
- **Stop-side pairing**: `AKS_AUTOSTOP_RESPECT_SB_QUEUE=true` (code default)
  ensures the evaluator never stops a cluster while the queue is non-empty,
  so the pair is symmetric: queue arrival ⇒ start, queue drain + idle window
  ⇒ stop.

## Cost note

Activating auto-start does not change the per-cycle cost — a burst that
needs the cluster always pays the start + init-ssd cost regardless of who
issues the start. What changes is the **wallclock latency** between the
first SB message and the start command (operator-in-the-loop → ~1s after
the next drain tick). The first BLAST job's E2E latency in a cold cycle
remains dominated by init-ssd (~30 min).

To shorten the cold-start latency itself, a separate follow-up is needed
(e.g. extend the auto-stop idle window, or schedule a periodic DB pre-warm
Job that re-runs init-ssd in the background just before expected bursts).

## Validation evidence

- `uv run pytest -q api/tests/test_control_plane_env.py api/tests/test_queue_autostart.py api/tests/test_idle_autostop_sb_queue.py api/tests/test_auto_stop_sb_signal.py` → 46 passed.
- Live behaviour (post-deploy):
  1. `az aks stop` the customer dev cluster, wait for `Stopped`.
  2. Enqueue one SB message via the dashboard `/api/settings/service-bus/send`.
  3. Within one drain tick (≤5s with `CELERY_BEAT_SERVICEBUS_DRAIN_SECONDS=5`)
     the worker should log `aks queue-autostart triggered cluster=...`
     and `az aks show ... powerState` flips to `Starting` then `Running`.
- A second SB message during the cooldown window must NOT re-issue start
  (lease holds for `SERVICEBUS_QUEUE_AUTOSTART_COOLDOWN_SECONDS`).

## Out of scope

- Cold-start latency reduction (DB pre-warm or auto-stop window extension)
  is tracked as the next follow-up item.
- The SPA already has no UI surface for the auto-start gate; the env-toggle
  remains the activation switch (a Settings UI is intentionally out of
  scope — a billable infra change should not be a one-click runtime flip).
