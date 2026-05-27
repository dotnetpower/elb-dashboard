# Public HTTPS: layered defense against ingress-nginx admission-webhook races

## Motivation

Settings → Public HTTPS → **Enable** failed on a cold cluster
(`elb-cluster-small`) with:

> kubectl apply Ingress failed: Error from server (InternalError): error
> when creating "STDIN": Internal error occurred: failed calling webhook
> "validate.nginx.ingress.kubernetes.io": failed to call webhook: Post
> "https://ingress-nginx-controller-admission.ingress-nginx.svc:443/networking/v1/ingresses?timeout=10s":
> no endpoints available for service "ingress-nginx-controller-admission"

This is the **7th** production fix to the Public HTTPS pipeline on
2026-05-27 (see other `2026-05-27-public-https-*.md` change notes in
this directory). The recurring failure mode is the same: a surface fix
patches one race, the next cluster cold-start surfaces the next race
in the chain. This change replaces the single-wait surface fix with
**layered defense** that covers every known race in the admission-
webhook bring-up sequence so subsequent cold-cluster cycles do not
surface yet another variant.

## Root cause analysis (every race, not just the visible one)

Between `kubectl apply -f ingress-nginx` and a successful
`kubectl apply -f <Ingress>` the apiserver must be able to call the
admission webhook successfully. That requires **all four** of:

1. **Controller Deployment Available** — at least one
   `ingress-nginx-controller` Pod is Ready.
2. **Admission bootstrap Jobs Complete** — the
   `ingress-nginx-admission-create` Job has minted the TLS Secret
   `ingress-nginx-admission`, and the `ingress-nginx-admission-patch`
   Job has injected the `caBundle` into the
   `ValidatingWebhookConfiguration`. Without (2), the apiserver
   rejects the webhook call with `x509: certificate signed by
   unknown authority` (a *different* failure shape from "no
   endpoints" but the same end-user symptom of a failed Enable
   click).
3. **EndpointSlice published** — the Service
   `ingress-nginx-controller-admission` has at least one address in
   its `subsets[].addresses[]`. The EndpointSlice controller is a
   **separate** kube-controller-manager controller that publishes
   asynchronously after Pod-Ready (typical lag: a few hundred ms on
   a warm cluster, several seconds on a cold AKS systempool). The
   "no endpoints available for service" error the operator saw is
   precisely this race.
4. **kube-proxy synced on the apiserver-side node** — even with the
   EndpointSlice published, the apiserver's TCP dial through
   kube-proxy can briefly fail on a Pod restart or a kube-proxy
   reconciliation. The webhook's `failurePolicy` defaults to `Fail`,
   so any one of these transient hits = whole `kubectl apply`
   bouncing.

The previous fix only addressed (1). On the next cold cluster (2) or
(3) would be the surfacing race. Without (4) the pipeline can still
bounce on a sub-second race even when (1)–(3) are all green.

## User-facing change

- The Public HTTPS Enable button no longer fails with
  webhook-related errors on first install, and is robust against
  Pod restarts / kube-proxy sync lag during install.
- Three new progress phases between `apply_cluster_issuer` and
  `apply_ingress`:
  - `wait_ingress_nginx_controller` — Deployment Available
  - `wait_admission_jobs_complete` — both bootstrap Jobs Complete
  - `wait_admission_endpoints_ready` — EndpointSlice published
- The final `apply_ingress` phase now silently retries up to 6
  times (5 s apart, ~30 s total) on the documented transient
  webhook error strings, then falls through to a clear error
  message if exhausted. Non-transient errors (RBAC denial, wrong
  CRD version, invalid Ingress YAML) still fail on the first
  attempt with their original message so the operator does not
  wait ~30 s for a definitively broken apply.

## API / IaC diff

- [api/services/k8s/ingress.py](../../../api/services/k8s/ingress.py) —
  added `INGRESS_NGINX_CONTROLLER_DEPLOYMENT` constant.
- [api/tasks/openapi/public_https.py](../../../api/tasks/openapi/public_https.py):
  - `_wait_for_ingress_nginx_controller()` — Deployment Available
    (cert-manager-webhook-style retry pattern, ~5 min budget).
  - `_wait_for_ingress_nginx_admission_jobs()` — wait both
    bootstrap Jobs Complete; benign-skip on NotFound (operator
    customised manifest), raise on real timeout.
  - `_wait_for_admission_endpoints_ready()` — poll
    `kubectl get endpoints ingress-nginx-controller-admission`
    `-o jsonpath={.subsets[*].addresses[*].ip}` until at least
    one address appears (~60 s budget, typical 1 probe on warm
    cluster).
  - `_apply_ingress_with_webhook_retry()` — final safety net.
    Retries on a documented allowlist of transient strings
    (`no endpoints available`, `connection refused`,
    `context deadline exceeded`, `i/o timeout`,
    `x509: certificate signed by unknown authority`,
    `tls: failed to verify certificate`, `EOF`); fails fast on
    anything else.
- No IaC change. No SPA change (new phases render through the
  existing raw-string display next to the spinner).

## Validation

```
cd /home/moonchoi/dev/elb-dashboard
uv run pytest -q api/tests/test_openapi_public_https.py
# 34 passed
uv run pytest -q api/tests
# 1549 passed
uv run ruff check api
# All checks passed!
```

New regression tests (one focused suite per helper, plus end-to-end
order assertions in the existing full-pipeline test):

- `test_wait_for_ingress_nginx_controller_*` (3 tests)
- `test_wait_for_ingress_nginx_admission_jobs_*` (3 tests:
  happy-path both Jobs, NotFound skip, real-timeout raise)
- `test_wait_for_admission_endpoints_ready_*` (3 tests:
  first-probe success, polls-until-IP, never-appears raise)
- `test_apply_ingress_with_webhook_retry_*` (3 tests: retry on
  transient, fail-fast on non-transient, exhaust + raise)
- Full-pipeline test now asserts the documented order
  Deployment → Jobs → Endpoints → Apply.

## Why this fix is different from the previous 6

Previous fixes patched a single observable failure each time. This
fix enumerates **every** race in the admission-webhook bring-up
sequence and adds a layer for each, plus a final retry safety net
for sub-second races that the layers above cannot eliminate. The
four layers map 1:1 to the four requirements in the root-cause
section. Should a future cold cluster surface yet another variant,
the variant is either (a) outside this code path entirely (e.g.
AKS API server unreachable, kubeconfig token expired) or (b) a
non-transient misconfiguration that the apply-retry function
deliberately does not mask.
