# Cluster Pulse Status Tone

## Motivation

The collapsed cluster pulse row used one health tone for both the primary cluster dot and the supporting status text. A slow `/api/blast` p95 metric could therefore make an otherwise healthy AKS cluster show an amber dot, which made the row look like the cluster itself was degraded.

## User-facing change

- The primary cluster dot now reflects AKS cluster health signals: lifecycle, power state, node readiness, node pressure, and hard resource pressure.
- API latency can still surface as an amber status line such as `API p95 3.6s`, but it no longer changes an otherwise healthy cluster dot away from green.
- Fully nominal rows now render the status line in the success color instead of muted grey.

## API / IaC diff summary

- No backend API change.
- No IaC change.
- Frontend-only presentation logic in `ClusterPulse`.

## Validation evidence

- `cd web && npm run build` completed successfully.
- Browser snapshot of the dashboard row showed `elb-cluster — API p95 3.5s` with the primary dot exposed as `Cluster healthy`; the dot rendered green while the API p95 status text remained amber.
