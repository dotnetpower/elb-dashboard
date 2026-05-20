# Dashboard

The Dashboard is the main operational view for the ElasticBLAST control plane. It shows the state of the Azure resources, runtime sidecars, BLAST databases, and recent jobs in one place.

## What To Check

- AKS cluster status and node pool readiness.
- Storage account network posture and BLAST database availability.
- ACR image status for the ElasticBLAST runtime images.
- Browser terminal availability.
- Recent BLAST jobs and their latest status.

## Screenshot Targets

Screenshots for this page are defined by these manifest targets:

- `dashboard-overview-desktop`
- `dashboard-mobile`

Add the captured images under `docs/images/screenshots/` after the demo environment has safe, representative data.# Dashboard

The Dashboard is the operator landing page for the ElasticBLAST control plane. It summarizes platform readiness across AKS, Storage, ACR, sidecars, and recent BLAST activity.

## Screenshot Slot

Capture target: `docs/images/screenshots/dashboard-overview.png`

Recommended state before capture:

- The local or deployed frontend is signed in.
- The platform cards have finished loading.
- AKS, Storage, ACR, and Jobs cards show meaningful data or intentional degraded states.
- Tenant, subscription, object, and account identifiers are masked if visible.

## Notes To Cover

- How to read platform readiness at a glance.
- Which card owns AKS, Storage, ACR, Terminal, and Jobs signals.
- What degraded states mean and where the user should go next.