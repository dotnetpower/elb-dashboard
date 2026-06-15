---
title: Configurable control-plane custom domain for the OpenAPI webhook
description: A Settings → Control plane domain section that persists the dashboard's public custom domain (e.g. dashboard.elasticblast.com) and feeds it to the ElasticBLAST OpenAPI sibling as CONTROL_PLANE_URL.
tags:
  - operate
  - blast
---

# Configurable control-plane custom domain

## Motivation

An operator created the DNS zone `dashboard.elasticblast.com` and wants the
control plane (the dashboard [Container App](https://learn.microsoft.com/azure/container-apps/overview))
to use it. The [ElasticBLAST](https://github.com/dotnetpower/elastic-blast-azure)
OpenAPI sibling running on [AKS](https://learn.microsoft.com/azure/aks/) webhooks
back to the dashboard (`CONTROL_PLANE_URL`); until now that URL was resolved only
from the `DASHBOARD_PUBLIC_URL` env or the auto-generated
`*.azurecontainerapps.io` FQDN — there was no UI to point it at a custom domain.

## User-facing change

New **Settings → Control plane domain** section:

- Text field to enter the custom domain (e.g. `https://dashboard.elasticblast.com`).
  Client- and server-side validation enforce the sibling's contract: `https://`
  only (except `localhost`), origin form, no path / query / fragment / credentials.
  The value is canonicalised (lower-cased scheme + host, rebuilt from the parsed
  components) and control characters are rejected, so a mixed-case scheme or a
  tab/newline injection can never survive into the stored webhook target.
- **Save domain** persists the value durably (survives Container App revision
  restarts) — no redeploy needed. **Clear** reverts to the FQDN fallback.
- An **Effective URL** row shows what the next OpenAPI deploy will actually inject
  and its `source` (`env` / `settings` / `container_app` / `none`). When a
  `DASHBOARD_PUBLIC_URL` env hard pin is present the section flags that it
  overrides the saved value.

The configured value is injected as `CONTROL_PLANE_URL` on the **next** OpenAPI
deploy (Settings → Public HTTPS / OpenAPI setup). It does not retro-patch a
running sibling.

## Resolution precedence

`api.services.control_plane_url.resolve_control_plane_url()` (used by the OpenAPI
deploy task):

1. `DASHBOARD_PUBLIC_URL` env — deploy-time hard pin (tests / private DNS).
2. **Settings value** — the new custom-domain field (durable singleton row).
3. `CONTAINER_APP_NAME` + `CONTAINER_APP_ENV_DNS_SUFFIX` — auto FQDN (default).
4. `""` — sibling webhook disabled.

## API / IaC diff summary

- New service `api/services/control_plane_url.py`: `normalise_control_plane_url`,
  `save_control_plane_url`, `get_control_plane_url`, `clear_control_plane_url`,
  `container_app_default_url`, `resolve_control_plane_url`. Durable-only
  (`dashboardsingletons` Table via the singleton store) — read at OpenAPI deploy
  time + Settings GET, so no Redis hot path.
- New routes `GET/PUT/DELETE /api/settings/control-plane`
  (`api/routes/settings/control_plane.py`), all `require_caller`-gated. PUT
  returns 503 when the durable store is unavailable so the SPA never shows a
  phantom save.
- `api/tasks/openapi/deploy.py::_resolve_control_plane_url` now delegates to the
  service (adds the Settings tier between env and the FQDN). Contract unchanged
  (still returns `str`).
- SPA: `web/src/api/settings.ts` typed client, new
  `ControlPlaneDomainSection.tsx`, wired into `SettingsPanel.tsx` +
  `useSettingsPanel.tsx` `VALID_SECTIONS`.
- No Bicep change — the value lives in the durable Storage Table, not env.

## Binding the domain to the Container App (separate one-time Azure step)

This change configures the **URL string** OpenAPI uses. Actually serving the
dashboard on `dashboard.elasticblast.com` (custom hostname + managed certificate
+ DNS records) is a separate Azure operation that cannot be validated locally —
run it once against the live deployment:

```bash
# 1. Add the hostname to the Container App (asserts domain ownership).
az containerapp hostname add \
  --resource-group rg-elb-dashboard \
  --name ca-elb-dashboard \
  --hostname dashboard.elasticblast.com

# 2. Create the DNS records the validation needs in the dashboard.elasticblast.com zone:
#    - CNAME dashboard -> <app FQDN>   (or A -> ingress IP for an apex)
#    - TXT  asuid.dashboard -> <customDomainVerificationId>
az network dns record-set cname set-record \
  --resource-group <dns-zone-rg> --zone-name elasticblast.com \
  --record-set-name dashboard --cname ca-elb-dashboard.<env>.azurecontainerapps.io

# 3. Bind a managed certificate (free, auto-renewed).
az containerapp hostname bind \
  --resource-group rg-elb-dashboard \
  --name ca-elb-dashboard \
  --hostname dashboard.elasticblast.com \
  --environment <managed-env-name> \
  --validation-method CNAME
```

After the cert is issued, set the same domain in **Settings → Control plane
domain** so the OpenAPI sibling webhooks back to it.

## Validation evidence

- `uv run pytest -q api/tests` → 3786 passed, 3 skipped (includes the new
  `api/tests/test_settings_control_plane.py`: normalisation, save/get/clear,
  env→settings→container_app precedence, route 400/503/200 contracts).
- `uv run ruff check` clean on all touched Python files.
- `cd web && npm run build` clean; `npm test -- --run` → 898 passed (includes
  `useSettingsPanel` section validation).
