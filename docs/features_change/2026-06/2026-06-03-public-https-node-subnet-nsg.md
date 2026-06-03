---
title: Public HTTPS — reconcile BYO node-subnet NSG for ingress LB inbound
date: 2026-06-03
tags:
  - openapi
  - networking
  - aks
---

# Public HTTPS — open the BYO node-subnet NSG to the ingress LoadBalancer

## Motivation

Enabling **Public HTTPS** on the OpenAPI endpoint failed on clusters that run in
the dashboard's bring-your-own (BYO) node subnet `vnet-elb-dashboard/snet-aks`.
cert-manager's Let's Encrypt HTTP-01 challenge timed out with *"Timeout during
connect (likely firewall problem)"* and the certificate never became Ready.

Root cause (confirmed live on `elb-cluster-02`, 2026-06-02): AKS's
cloud-controller-manager writes the `Internet -> 80/443` LoadBalancer inbound
allow rule **only to the NIC/cluster NSG in the `MC_` node resource group**, never
to a BYO subnet NSG. AKS auto-attaches an NSG
(`vnet-elb-dashboard-snet-aks-nsg-<region>`) to the BYO subnet with only the
default rules, whose `DenyAllInBound` (priority 65500) silently drops inbound
80/443. Inbound is evaluated against the subnet NSG too, so external traffic to
the ingress LB VIP timed out on both ports — while the internal LB kept working
because the default `AllowVnetInBound` permits intra-VNet traffic. Everything
else (NIC NSG rule, LB rules/frontend/probe, controller pod, nodePorts,
healthcheck, `externalTrafficPolicy`) was already correct.

## User-facing change

Enabling Public HTTPS now succeeds end-to-end on BYO-subnet clusters without any
manual `az network nsg rule` step. The pipeline reconciles the node-subnet NSG
automatically right after the ingress LB VIP is known.

## API / IaC diff summary

- New service module `api/services/aks/node_subnet_nsg.py`:
  - `first_node_subnet_id(cluster)` — first non-empty agent-pool
    `vnet_subnet_id`, or `""` for managed-VNet clusters.
  - `ensure_ingress_lb_inbound_rule(...)` — idempotent
    `begin_create_or_update` of a fixed-name rule
    `allow-ingress-nginx-http-https` (priority 500, `Internet -> <LB VIP>`
    TCP 80/443). Gracefully **skips** (no-op) for managed-VNet clusters
    (`reason=managed_vnet`) and BYO subnets without an NSG
    (`reason=no_subnet_nsg`), so it can never regress an already-working
    cluster. Rule is destination-scoped to the exact LB VIP — never a wider
    surface.
- `api/tasks/openapi/public_https.py`: new best-effort **Step 3b**
  (`ensure_node_subnet_nsg` progress marker) between the LB-IP wait (Step 3)
  and cert-manager install (Step 4). On failure it logs and continues; the
  later certificate-ready wait still surfaces a timeout with diagnostics rather
  than aborting the pipeline at the NSG step.
- No Bicep change: `infra/modules/network.bicep` defines `snet-aks` with no NSG;
  AKS attaches the NSG at cluster-create time with an AKS-generated name/region,
  so the reconcile belongs in the pipeline (runtime), not the template. The
  shared user-assigned MI already has NSG management in `rg-elb-dashboard`
  (`controlPlaneRoles.bicep`).

## Validation evidence

- `uv run ruff check api/services/aks api/tasks/openapi/public_https.py
  api/tests/test_node_subnet_nsg.py` → clean.
- `uv run pytest -q api/tests/test_node_subnet_nsg.py
  api/tests/test_openapi_public_https.py` → 41 passed.
- Full suite `uv run pytest -q api/tests` → 2485 passed, 1 unrelated
  pre-existing subprocess-timeout flake (`test_terminal_exec.py::
  test_run_truncates_stdout_above_cap`, does not touch the changed modules).
- Live fix applied manually on `elb-cluster-02` to unblock the user:
  `port80/443 OPEN`, ACME path `HTTP 308`, certificate `Ready=True`, order
  `valid`, and `https://elb-openapi-0858f97bac.koreacentral.cloudapp.azure.com/`
  served a valid Let's Encrypt cert (issuer `CN=YR2`, `ssl_verify=0`). The code
  change makes this automatic for future enablements.
