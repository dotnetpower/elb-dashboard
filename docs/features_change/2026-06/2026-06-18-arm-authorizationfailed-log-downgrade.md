---
title: Expected out-of-scope AuthorizationFailed on ARM discovery logs without a stack
description: AuthorizationFailed on list_resource_groups is treated as an expected out-of-scope result and logged as a one-line warning instead of an App Insights exception row.
tags:
  - operate
  - security
---

# ARM discovery AuthorizationFailed log downgrade (#46)

## Motivation

`GET /api/arm/.../resource-groups` calls `resource_groups.list()` under the
shared user-assigned managed identity. The route already degrades to `[]` on
failure, but it logged with `exc_info=True`, so each `AuthorizationFailed`
recorded a full App Insights exception row (9 events / 7 days).

## Root-cause classification

This is the **expected out-of-scope** case, not a missing-grant bug. The
dashboard lets a user point at any subscription they enter, and the shared MI is
not guaranteed to hold a read role on every subscription. An `AuthorizationFailed`
on a discovery read is therefore an already-handled outcome (empty list), not a
server fault. (If the product later decides the MI should read resource groups
subscription-wide, that is a separate §12a Rule 1 *phase-1* additive role grant +
capability-probe change — intentionally not bundled here.)

## User-facing change

None. The resource-group picker still degrades to an empty list when the MI lacks
scope. The handled 403 stops producing App Insights exception rows.

## API / IaC diff summary

- `api/routes/arm.py`:
  - New `_is_expected_authorization_failure(exc)` — true for an ARM
    `HttpResponseError` with status 403 or `error.code == "AuthorizationFailed"`.
  - New `_log_discovery_failure(operation, exc)` — logs a one-line warning
    (no stack) for the expected AuthorizationFailed case, and keeps the full
    `exc_info` trace for genuine faults.
  - `list_resource_groups` now routes its failure log through that helper.

No infra / RBAC change.

## Validation evidence

- `uv run pytest -q api/tests/test_arm_discovery_logging.py` — 3 passed:
  classification of 403 / `AuthorizationFailed` vs 500 / non-Azure errors; the
  AuthorizationFailed path logs with `exc_info is None`; a genuine 500 keeps its
  stack.
- `uv run ruff check` clean on the touched files.
