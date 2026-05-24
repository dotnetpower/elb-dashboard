# Status Message Truthfulness

## Motivation

Several long-running dashboard surfaces used optimistic or synthetic wording that could imply real backend progress before Azure or Celery had actually reached that state. This was especially risky for operations where validation or worker failures can happen before the underlying Azure action starts.

## User-facing change

- ACR image builds now distinguish a queued worker task from an active ACR run. The dashboard shows `Build task queued` until the server observes ACR run activity, then switches to active ACR wording.
- AKS start, stop, and delete transitions now retain and poll the returned Celery task id. Failed, revoked, or timed-out lifecycle tasks clear the transition label and surface an error instead of leaving a stale `Starting`, `Stopping`, or `Deleting` state.
- AKS start estimates now read as estimates / expected sequence instead of describing inferred live progress as if it were observed state.
- Warmup progress labels now match the actual Celery phases emitted by the warmup task (`checking_storage`, `sharding`, `planning_node_warmup`, `applying_warmup_jobs`, `warming_nodes`) instead of the retired Durable Functions phase names.
- The OpenAPI deployment success-discovery banner no longer says the pod is starting after the backend has already verified ready replicas. It now says the deployment is ready and the dashboard is refreshing service discovery.
- Live Wall sidecar log mock lines are now restricted to documentation mock-preview mode. In the real app, missing log-stream routes show polling/no-activity rather than invented operational logs.

## API / IaC diff summary

- Frontend only for message/state handling, except for consuming the already-returned AKS lifecycle `task_id` in typed clients.
- No infrastructure changes.

## Validation evidence

- `npm run build`
