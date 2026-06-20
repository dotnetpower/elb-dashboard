---
title: Durable fix + readable display for exp-skip-warmed-ssd-init submit failure
description: Bake the ELB_OPENAPI_SKIP_WARMED_SSD_INIT=0 escape hatch into the dashboard-applied elb-openapi manifest so BLAST submits survive cluster restarts, and render the Message Flow job-detail error full-width instead of squished in a narrow grid cell.
tags:
  - blast
  - ui
---

# exp-skip-warmed-ssd-init submit failure — durable fix + readable error

## Motivation

A `servicebus`-submitted `blastn` job (`94f2076d1391`) failed at submit with:

> The command "elastic-blast submit --cfg …/config.ini" returned with exit code 1
> ERROR: Unrecognized configuration parameter "exp-skip-warmed-ssd-init" in
> section "cluster".

Two problems:

1. **Root cause (recurring).** The published `elb-openapi:4.24` image is
   split-versioned — its server writes the `[cluster] exp-skip-warmed-ssd-init`
   config key, but the elastic-blast CLI bundled in the **same** image predates
   the `CFG_CLUSTER_EXP_SKIP_WARMED_SSD_INIT` constant, so `configparser`
   rejects the generated INI and every submit exits 1. The documented escape
   hatch (`ELB_OPENAPI_SKIP_WARMED_SSD_INIT=0`) had only ever been applied as a
   one-off `kubectl set env`, which is **wiped whenever the pod is recreated**
   (cluster stop/start, redeploy) — so it silently regressed after the cluster
   restarted.
2. **Display.** The dashboard's Message Flow job-detail modal rendered the error
   as one cell of an `auto-fit minmax(120px, 1fr)` summary grid, so the long
   error string wrapped inside a ~120px column and looked broken.

## User-facing change

* BLAST submits no longer fail with the `exp-skip-warmed-ssd-init` error: the
  escape hatch is now part of the deployment the dashboard applies, so it
  survives cluster restarts and redeploys (no manual `kubectl` needed).
* The Message Flow job-detail **Error** is rendered as a full-width, wrapping,
  monospace block (with a subtle danger border) below the summary grid, instead
  of being squeezed into a narrow summary cell.

## API/IaC diff summary

* [api/tasks/openapi/manifests.py](../../../api/tasks/openapi/manifests.py) —
  added `{"name": "ELB_OPENAPI_SKIP_WARMED_SSD_INIT", "value": "0"}` to the
  elb-openapi deployment env, with a comment pointing at the sibling-image
  follow-up (bump `IMAGE_TAGS["elb-openapi"]` once the image bundles a CLI that
  knows the key, then drop this).
* [web/src/components/cards/MessageFlow/MessageFlowModal.tsx](../../../web/src/components/cards/MessageFlow/MessageFlowModal.tsx)
  — moved `error_code` out of the summary grid into a dedicated full-width block.
* Operational: applied `kubectl set env deploy/elb-openapi
  ELB_OPENAPI_SKIP_WARMED_SSD_INIT=0` to the live cluster for the immediate
  unblock (the manifest change makes it durable on the next dashboard deploy).

## Validation evidence

* `uv run pytest -q api/tests/test_openapi_task.py
  api/tests/test_openapi_deployment.py` → 21 passed (new assertion:
  `env["ELB_OPENAPI_SKIP_WARMED_SSD_INIT"] == "0"`). `ruff check` clean.
* `cd web && npm run build` → built clean; `npx eslint` on the modal → clean.
* Live: escape hatch applied + rollout verified on the customer cluster
  (`ELB_OPENAPI_SKIP_WARMED_SSD_INIT=0` confirmed on the new pod).
