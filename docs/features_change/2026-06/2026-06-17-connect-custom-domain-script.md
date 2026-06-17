# Connect a custom domain to the dashboard Container App (operator automation)

**Date:** 2026-06-17
**Area:** Operations (`scripts/dev/connect-control-plane-domain.sh`)

## Motivation

Saving a value in **Settings → Control plane domain** persists the
`CONTROL_PLANE_URL` (the host the `elb-openapi` sibling webhooks back to) but
does **not** make the dashboard reachable on that host — there was no code or
operator action that binds a custom domain to the dashboard Container App.
Result: "it saves but nothing actually connects."

Two distinct things were conflated:

1. **`CONTROL_PLANE_URL`** (Settings value) — where the sibling sends webhooks.
2. **Serving the dashboard on a branded host** — requires binding a custom
   domain + managed certificate to the Container App ingress, plus DNS. This
   part had no implementation.

## What shipped

`scripts/dev/connect-control-plane-domain.sh` — an idempotent operator script
that performs the real connection without any new managed-identity RBAC and
without ever risking the running app's ingress (it only *adds* a custom domain;
the auto-generated `*.azurecontainerapps.io` FQDN keeps working):

1. Reads the Container App FQDN, environment static IP, and
   `customDomainVerificationId`.
2. Auto-detects the Azure DNS zone (apex vs sub-domain) and upserts:
   - ownership `TXT asuid[.<label>]` → verification id;
   - routing `A @` → env static IP (apex) or `CNAME <label>` → app FQDN (sub).
3. **Public-DNS gate** — if the domain does not resolve publicly yet (its
   registrar nameservers are not pointed at Azure DNS), it prints the exact
   Azure nameservers to set at the registrar and STOPS before the bind, because
   a managed certificate cannot be issued until public DNS resolves.
4. Binds the hostname with a free Azure-managed certificate
   (`az containerapp hostname add` + `bind --validation-method`).

Supports `--dry-run`, `--force`, and `--domain/--zone/--validation` overrides.

## Why a script (not a Settings button or untested Bicep)

- A Settings-triggered runtime path would require giving the api sidecar's
  managed identity write access to its own Container App (privilege escalation +
  charter §12a 2-phase RBAC) and risks the app modifying its own ingress.
- A Bicep `customDomains` + managed-certificate block cannot be validated without
  live public DNS and would fail `azd provision` if the domain is not yet
  resolvable.
- The script needs no new RBAC, is safe to re-run, and refuses to attempt a
  doomed bind — the lowest-risk way to perform the real connection.

## Known prerequisite (not code — registrar action)

For the bundled deployment, `elasticblast.com` is an externally-registered domain
whose registrar nameservers are **not** pointed at Azure DNS (public DNS returns
NXDOMAIN). Creating the Azure DNS zone does not delegate the domain. Until the
registrar nameservers are set to the zone's Azure nameservers, no custom domain
(dashboard or OpenAPI) can be issued a certificate. The script surfaces the exact
nameservers to set.

## Validation

- `bash -n` clean; `--dry-run` against `dashboard.elasticblast.com` reads the live
  Container App, auto-detects the `dashboard.elasticblast.com` apex zone, prints
  the DNS upserts, and correctly hits the public-DNS gate (exit 2) with the
  registrar nameserver guidance — confirming the flow end-to-end up to the gate.
- Live certificate bind is intentionally not exercised (blocked on the registrar
  delegation above).
