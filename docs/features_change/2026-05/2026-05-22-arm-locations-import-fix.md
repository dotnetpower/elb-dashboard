# 2026-05-22 — `/api/arm/.../locations` ModuleNotFoundError fix

## Motivation

`api/routes/arm.py::list_locations` imported `from azure.mgmt.subscription import SubscriptionClient`, but `azure-mgmt-subscription` is not pinned in `pyproject.toml`. The deployed image happened to carry it transitively, but a fresh local `uv sync` does not. Result: `GET /api/arm/subscriptions/<id>/locations` raised `ModuleNotFoundError: No module named 'azure.mgmt.subscription'` and the SPA's region picker fell back to its bundled list with no diagnostic.

## User-facing change

`GET /api/arm/subscriptions/{subscription_id}/locations` returns the live subscription location list again. No SPA change.

## API / IaC diff summary

* [api/routes/arm.py](../../../api/routes/arm.py): swap the lazy import to `from azure.mgmt.resource import SubscriptionClient`. `azure-mgmt-resource==23.2.0` is already pinned and re-exports the same SDK-generated `SubscriptionClient` (`azure.mgmt.resource.subscriptions._subscription_client.SubscriptionClient`). The method surface used by this route (`client.subscriptions.list_locations(subscription_id)`) is identical.
* No new dependency; no `pyproject.toml` / `uv.lock` change.

## Validation evidence

```text
$ uv run ruff check api/routes/arm.py
All checks passed!

$ uv run pytest -q api/tests -k arm
94 passed, 1166 deselected in 3.50s

# Live route after restart (auth bypass disabled — 401 means it reached the auth gate cleanly):
$ curl -s -o /tmp/loc.out -w 'HTTP:%{http_code}\n' \
    http://127.0.0.1:8085/api/arm/subscriptions/<sub>/locations
HTTP:401
{"detail":"missing bearer token"}

# Before the fix the same request returned 500 with
# `ModuleNotFoundError: No module named 'azure.mgmt.subscription'` in the api log.
$ grep -c ModuleNotFoundError .logs/local/latest/api.log
0
```
