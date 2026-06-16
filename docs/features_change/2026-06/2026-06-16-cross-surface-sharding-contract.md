---
title: Cross-surface BLAST sharding contract convergence
description: Dashboard, external submit, and Service Bus BLAST requests now share one sharding/search-space resolver and one calibrated search-space policy.
tags:
  - blast
  - architecture
  - operate
---

# Cross-surface BLAST sharding contract convergence

## Motivation

BLAST submits could drift across the dashboard inline-FASTA path, the external
submit facade, and the optional [Azure Service Bus](https://learn.microsoft.com/azure/service-bus-messaging/service-bus-messaging-overview)
ingress. The highest-risk drift was `searchsp` handling: one surface could trust
caller input while another derived the calibrated Web BLAST value server-side.

## User-facing change

- Dashboard inline-FASTA submits, `/api/v1/elastic-blast/submit`, and Service
  Bus drain now resolve sharding mode and `db_effective_search_space` through
  the same server-side helper.
- Calibrated databases such as `core_nt` derive the Web BLAST-compatible
  search space server-side for all three surfaces.
- A bad caller override is rejected on dashboard / OpenAPI submits and stripped
  plus downgraded on the Service Bus path instead of being trusted blindly.

Persona impact: Reader/Contributor/Owner behavior is unchanged because this is a
submit-contract hardening change only; no auth, RBAC, or routing surface changed.

## API / IaC diff summary

- Added `sharding_mode` and `db_effective_search_space` to the external submit
  options model.
- Updated the Service Bus request-contract documentation to match the external
  submit shape.
- No infrastructure defaults changed; the existing capacity gate remains
  default-OFF.

## Validation evidence

- `pytest -q api/tests/test_external_blast_api.py api/tests/test_blast_submit_route_options.py api/tests/test_servicebus_tasks.py`
- `pytest -q api/tests/test_external_blast_api.py api/tests/test_blast_submit_route_options.py api/tests/test_blast_queue.py api/tests/test_blast_tasks.py api/tests/test_openapi_rate_limit.py`
