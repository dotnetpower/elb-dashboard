# Docs: glossary page for Azure / Kubernetes / project abbreviations

## Motivation

Researchers and first-time operators landing on this site are often unfamiliar
with Azure-specific abbreviations (MI, UAMI, MSAL, SAS, RBAC, …), Kubernetes
shorthand (PVC, K8s, Pod, Job), and project-internal terms (sidecar, control
plane vs workload plane, ttyd, elb-openapi). The docs use these names freely
in `index.md`, `architecture/*`, `copilot/*`, and the user guide, but there
was no single page that defined them.

## User-facing change

New page **Overview → Glossary** (`docs/glossary.md`) lists every abbreviation
and product name used across the documentation, grouped by domain:

- **Azure platform** — AKS, ACR, ACA/Container Apps, ARM, azd, Bicep, Entra,
  Key Vault, Storage, Table Storage, App Registration, App Insights.
- **Identity & auth** — MI, UAMI, SAMI, MSAL, OAuth/OIDC, PKCE, JWT, RBAC,
  SP, OBO, SAS, dev-bypass.
- **Networking** — VNet, Subnet, PE, NSG, DNS, FQDN, TLS, CORS, WebSocket/WSS,
  CIDR.
- **Kubernetes** — K8s, Pod, Job, Deployment/StatefulSet, PVC, Ingress,
  kubeconfig.
- **BLAST domain** — BLAST, ElasticBLAST, NCBI, OpenAPI/Swagger, SSE.
- **Project-specific** — control plane, workload plane, sidecar, ttyd,
  Celery, beat, Redis, SPA, CLI, azcopy, elb-openapi, IaC, SemVer.

Each entry uses the `def_list` markdown extension (already enabled) so the
page renders as a clean term/definition list. Entries link to Microsoft Learn,
Kubernetes, NCBI, or in-repo references where the reader is likely to need
more depth.

## API / IaC diff summary

- **No code / API / Bicep changes.**
- `docs/glossary.md` — new file (~150 def-list entries across 6 sections).
- `mkdocs.yml` — added `Glossary: glossary.md` under the **Overview** tab,
  placed between Troubleshooting and Change Log.

## Validation evidence

- `cd web && npm run build` — unaffected (docs-only change).
- Nav verified by reading the updated `mkdocs.yml`; no other pages reference
  `glossary.md` so there is no broken-link risk to existing redirects.
- All cross-page links from the glossary (`architecture/high-level.md`,
  `architecture/identity.md`, `architecture/storage-contract.md`,
  `copilot/version-management.md`, `copilot/browser-terminal.md`,
  `get-started.md`, `tags.md`) point at files that already exist in
  `docs/`.
