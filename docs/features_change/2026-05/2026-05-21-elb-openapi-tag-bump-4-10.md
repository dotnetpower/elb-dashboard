# Bump elb-openapi image tag to 4.10

## Motivation

The sibling `dotnetpower/elastic-blast-azure` change of 2026-05-21 adds the
`content=full|merged|xml` query parameter to `GET /v1/jobs/{job_id}/results`
in `docker-openapi/app/main.py`. To roll that code out to the running AKS
deployment we need a new image tag — `kubectl apply` only triggers a
rollout when `spec.template.spec.containers[*].image` changes, and the
deployment manifest sets `imagePullPolicy: Always` but does not stamp a
re-deploy annotation. Without a fresh tag the same `4.9` reference would
be re-applied as a no-op even after pushing new layers to ACR.

## User-facing change

None directly — this is the deployment plumbing for the previously
shipped `?content=` mode (see
[2026-05-21-results-content-mode.md](./2026-05-21-results-content-mode.md)).
After the rollout completes the API Reference Try It will surface the
`content` query parameter as a dropdown (`full` default, `merged`, `xml`),
matching the sibling spec.

## API / IaC diff summary

- [api/services/image_tags.py](../../../api/services/image_tags.py) —
  `IMAGE_TAGS["elb-openapi"]` bumped from `"4.9"` to `"4.10"`. No other
  callsite needs an update: `api/tasks/openapi/__init__.py` reads via
  `IMAGE_TAGS.get("elb-openapi", "4.9")` (the literal there is only a
  defensive fallback if the dict were ever empty, which it is not).
- No infra change. No Bicep change. No test fixture change — existing
  openapi tests pin arbitrary tags ("4.9") that are independent of the
  live `IMAGE_TAGS` mapping.

Cross-repo rollout sequence (executed in this session):

1. `az acr build --registry elbacr01 --image elb-openapi:4.10 --file Dockerfile
    https://github.com/dotnetpower/elastic-blast-azure.git#master:docker-openapi`
2. `kubectl set image deployment/elb-openapi openapi=elbacr01.azurecr.io/elb-openapi:4.10 -n default`
3. `kubectl rollout status deployment/elb-openapi -n default`

## Validation

- `uv run pytest -q api/tests/test_openapi_deployment.py api/tests/test_openapi_task.py`
  — 5 passed.
- Manual: `curl http://127.0.0.1:8090/openapi.json | jq '.paths."/v1/jobs/{job_id}/results".get.parameters'`
  after rollout — confirms the new `content` parameter is published.
- Manual: hit `GET /v1/jobs/<known-job>/results?content=xml` against
  a job whose merger has already published `merged_results.out.gz`,
  expect `application/xml` (gunzipped BLAST XML) with HTTP 200; the
  same call with `content=merged` returns a small ZIP; default
  (no `content`) preserves the legacy behaviour.

## Rollout notes

- Same-tag-after-rebuild would have been silently no-op because the
  applied Deployment spec stays byte-identical. Bumping the tag is the
  least-surprise option and keeps a rollback path (revert to `4.9` if
  needed).
- Charter §13 redeploy rule: this is **not** an elb-dashboard sidecar
  change. The redeploy here is the AKS `elb-openapi` workload that
  hosts the public BLAST API; that workload has its own release
  pipeline (`/api/acr/build` + `/api/aks/openapi/deploy`). No
  `azd provision`, no `quick-deploy.sh`, no `postprovision.sh` ran.
