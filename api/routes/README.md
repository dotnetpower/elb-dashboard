# api/routes/

FastAPI routers. Wired in [api/main.py](../main.py) — registration order is
load-bearing (any new `/api/*` router MUST be `include_router`-ed **before**
[frontend_proxy.py](./frontend_proxy.py); otherwise the catch-all swallows it).

For the full prefix → file table see
[docs/copilot/codebase-map.md §1](../../docs/copilot/codebase-map.md#1-backend-route-map-apiroutes).

## File index

| File | Prefix | One line |
|------|--------|----------|
| [health.py](./health.py) | `/api/health` | Liveness + readiness + Celery diag endpoints. No auth. |
| [me.py](./me.py) | `/api/me` | MSAL bearer → `CallerIdentity`. Used by SPA bootstrap. |
| [monitor/](./monitor/) | `/api/monitor` | Read-only dashboard polling package. AKS, metrics, storage, ACR, jobs, and sidecars live in focused submodules. Wrapped in `_graceful` — must never 500. |
| [arm.py](./arm.py) | `/api/arm` | ARM proxy under shared MI (subscriptions, RGs, storage accounts, ACRs, VMs). |
| [resources.py](./resources.py) | `/api/resources` | Synchronous wizard provisioning (ensure-rg / -storage / -acr). |
| [storage/](./storage/) | `/api/storage` | Storage package — `prepare-db` and local-debug IP-allowlist toggle (charter §9) live in focused submodules. |
| [elastic_blast.py](./elastic_blast.py) | `/api/v1/elastic-blast` | External submit/jobs facade. |
| [terminal_ws.py](./terminal_ws.py) | `/api/terminal` | Ticket + WebSocket → loopback ttyd. MSAL on handshake. |
| [terminal_legacy.py](./terminal_legacy.py) | `/api/terminal/{vm}/*` | **HTTP 410 by design** — VM model is retired. |
| [tasks.py](./tasks.py) | `/api/tasks` | Celery `AsyncResult` polling. |
| [aks/](./aks/) | `/api/aks` | `aks_router` package — SKUs, provisioning, OpenAPI proxy/deploy, lifecycle, and role assignment live in focused submodules. |
| [acr.py](./acr.py) | `/api/acr` | `acr_build_router` — ACR build dispatch. |
| [blast/](./blast/) | `/api/blast` | `blast_router` package — jobs, submit, databases, taxonomy, schedules, and result analytics live in focused submodules. Shared HTTP helpers remain in [_blast_shared.py](./_blast_shared.py). |
| [warmup.py](./warmup.py) | `/api/warmup` | `warmup_router` — DB warmup planning + status. |
| [audit.py](./audit.py) | `/api/audit` | `audit_router` — append-blob audit log. |
| [frontend_proxy.py](./frontend_proxy.py) | `/*` catch-all | Reverse-proxy to frontend sidecar `127.0.0.1:8081`. **Stays last.** |

> The monolithic `stubs.py` (503-only) was split into the per-domain routers above in 2026-05-19.

## When you add a route

1. Pick the right router file (or create a new one with a `/api/...` prefix).
2. Add the `include_router` line in [api/main.py](../main.py) **above** the
   `frontend_proxy.router` include.
3. Wire the typed client in [web/src/api/endpoints.ts](../../web/src/api/endpoints.ts).
4. Update [docs/copilot/codebase-map.md](../../docs/copilot/codebase-map.md) in
   the same change.
5. Validate: `uv run pytest -q api/tests` + curl smoke (charter §13).
