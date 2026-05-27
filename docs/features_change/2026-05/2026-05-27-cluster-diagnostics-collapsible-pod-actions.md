# Cluster Diagnostics — collapsible Node Resources, icon-only pod actions, pod delete

## Motivation

The AKS cluster details modal had three usability rough edges reported from
the demo:

1. **Node Resources** could not be collapsed. Once the cluster had a few
   pools the section pushed the rest of the diagnostics (Nodes / Active
   Pods / Run kubectl) below the fold.
2. **Active Pods** action buttons (`Logs` / `Describe`) wore their text
   label and so each row's actions column was wider than the rest of the
   row, breaking visual rhythm. No way to **delete** a stuck pod from the
   UI either — operators had to drop into the browser terminal and run
   `kubectl delete pod`.
3. The **Run kubectl command** output (and a few other text blobs in the
   modal) was centered — `.glass-dialog` sets `text-align: center` for
   the confirm-dialog family and the cluster details modal reuses that
   class for the glass surface.
4. The `Run kubectl command` proxy only accepted the plural resource form
   (`get pods` / `get nodes` / `top nodes`). `kubectl` itself treats
   singular and plural interchangeably and people muscle-memory the
   singular form.

## User-facing change

* **Node Resources** is now collapsible (chevron on the left of the
  header, matching Nodes / Active Pods). It is **expanded by default** —
  the section is the most common reason the modal is opened.
* **Active Pods** Logs / Describe / Delete buttons are now icon-only
  square buttons. The text label has been moved into the `title` tooltip
  and an `aria-label` (screen-reader contract preserved).
* **Delete pod** action — `Trash2` icon. Shown only when the pod's
  namespace is **not** in `SYSTEM_NAMESPACES` (`kube-system`,
  `kube-public`, `kube-node-lease`, `gatekeeper-system`, `azure-arc`,
  `calico-system`, `tigera-operator`). Click opens a glass `ConfirmDialog`
  that explains the controller-recreation semantics; the dialog stays
  open and surfaces the error if the DELETE fails.
* **Result text is left-aligned.** The `cluster-detail-modal-body`
  wrapper now sets `text-align: left` explicitly, overriding the
  `.glass-dialog` default.
* **`get pod` / `get node` / `top node` (singular)** are now valid input
  for the in-page kubectl runner.
* **AKS card → Active jobs rows are now single-line.** The previous
  two-line layout (title above + `program · db` meta below + colored
  state pill + split bar) has been replaced with a pipe-separated
  single line:
  `{program} | {db} | {title} | {STATE} | {age}(age) | {duration}(duration)`.
  The row keeps its state-tinted background and now also a 3-px
  state-colored left edge bar so the state remains scannable without
  the pill. `age` is wall-clock time since `created_at` (keeps ticking
  for terminal jobs); `duration` is the compute time (`elapsedSec`)
  which freezes when the job reaches a terminal state. Full title is in
  the row's `title` tooltip when truncated.

## API / IaC diff summary

### Backend

* New service helper `api/services/k8s/observability.k8s_pod_delete()` —
  posts `DELETE /api/v1/namespaces/{ns}/pods/{pod}` via the existing
  authenticated session, with `propagationPolicy=Background` and a
  bounded `gracePeriodSeconds` (default 30, clamped to `[0, 600]`).
* New constant `SYSTEM_NAMESPACES: frozenset[str]` — single source of
  truth for the namespaces the dashboard will never touch. Frontend
  hides the Delete button for the same set, but the helper raises
  `PermissionError` if a hand-crafted call reaches it (OWASP A01
  defence-in-depth).
* New route `DELETE /api/monitor/aks/pod` — gates on `SYSTEM_NAMESPACES`
  before invoking the helper, maps the helper's `PermissionError` /
  `ValueError` to 403 / 400, and invalidates the
  `monitor:aks:pods:<sub>:<rg>:<cluster>` snapshot prefix so the next
  poll returns fresh state.
* `POST /api/monitor/aks/run-command` now accepts the singular resource
  form (`get pod`, `get node`, `top node`) in addition to the plural
  form. Also strips a leading `kubectl ` once and re-uses the same
  matcher for both prefixed and bare verbs.

### Frontend

* `monitoringApi.k8sPodDelete()` — new typed client (`api/del`).
* `K8sPodsSection.tsx` — icon-only buttons, delete handler + confirm
  dialog + post-delete refetch.
* `NodeResourcesSection.tsx` — collapsible header (defaults to
  expanded).
* `DetailsModal.tsx` — `text-align: left` on the scrollable body.
* `ClusterBento/atoms.tsx` — `JobRow` rewritten as a single-line
  pipe-separated layout (no more two-line title/meta + state pill +
  split bar). Drops the in-row dependency on `BlastJobIdentity`,
  `SplitProgress`, and `JobStateBadge`; those exports are kept for the
  modal / other surfaces.

### IaC

None — pure code change.

## Validation evidence

* `uv run pytest -q api/tests/test_k8s_pod_delete.py` — **12 passed** in
  ~2.5 s. New tests cover the system-namespace gate (parametrised across
  all 7 system namespaces), invalid-name rejection, grace-period range,
  202 → `deleted`, 404 → `not_found`, and 5xx → `error` with detail
  pass-through.
* `uv run pytest -q api/tests/test_route_contracts.py
  api/tests/test_monitor_cache.py
  api/tests/test_services_facade_contract.py
  api/tests/test_k8s_pod_describe.py
  api/tests/test_k8s_list_events.py` — **180 passed**.
* `uv run ruff check api/routes/monitor/aks.py
  api/services/k8s/observability.py api/services/k8s/monitoring.py
  api/services/monitoring/__init__.py api/tests/test_k8s_pod_delete.py`
  — clean.
* `npx tsc --noEmit` (web/) — clean.
* `npx eslint` on
  `src/components/ClusterDiagnostics/K8sPodsSection.tsx`,
  `src/components/ClusterDiagnostics/NodeResourcesSection.tsx`,
  `src/components/ClusterDetailModal/DetailsModal.tsx`,
  `src/components/cards/ClusterBento/atoms.tsx`,
  `src/api/monitoring.ts` — clean.
* `npx vitest run src/components/cards/ClusterBento/jobMapping.test.ts`
  — 9 passed. The `JobRow` data contract is unchanged; only the
  rendered DOM shape changed.
