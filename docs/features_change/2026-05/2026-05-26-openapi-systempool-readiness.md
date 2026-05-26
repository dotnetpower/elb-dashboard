# OpenAPI Deploy — systempool readiness diagnostics

## Motivation

Deploying `elb-openapi` immediately after enabling [Azure Kubernetes Service](https://learn.microsoft.com/azure/aks/intro-kubernetes) [Workload Identity](https://learn.microsoft.com/azure/aks/workload-identity-overview) can fail with a generic `no pod reached Ready` message. The live failure showed `azure-wi-webhook-controller-manager` stuck Pending because the single `Standard_D2s_v3` systempool was already at 98% requested CPU, leaving no room for the webhook.

## User-facing change

New AKS clusters default the systempool to 2 nodes instead of 1, while keeping the same default SKU. OpenAPI deploy failures now include Kubernetes warning diagnostics and classify the Workload Identity webhook unavailable case with a concrete remediation: increase systempool capacity, then re-run Deploy elb-openapi.

## API / IaC diff summary

- `GET /api/aks/skus` now returns `default_system_node_count`.
- `/api/aks/provision`, `/api/aks/preflight`, the Celery provision task, and the SPA provision modal default to systempool node count 2.
- `api.tasks.openapi.deploy_openapi_service` adds `openapi_deploy.diagnostics` with `likely_cause`, `message`, and selected warning events when Ready replicas never appear.
- No Bicep changes.

## Validation evidence

- Live cluster evidence: `azure-wi-webhook-controller-manager` Pending; scheduler reported `0/11 nodes are available: 1 Insufficient cpu, 10 node(s) had untolerated taint(s)`.
- `uv run pytest -q api/tests/test_openapi_deploy_contract.py api/tests/test_aks_skus.py api/tests/test_azure_provision_aks.py` — 25 passed.
- `uv run ruff check api/tasks/openapi/deploy.py api/services/aks_skus.py api/routes/aks/provision.py api/routes/aks/preflight.py api/tests/test_openapi_deploy_contract.py api/tests/test_aks_skus.py api/tests/test_azure_provision_aks.py` — passed.
- `npm run build` in `web/` — passed; Vite reported existing large chunk warnings only.