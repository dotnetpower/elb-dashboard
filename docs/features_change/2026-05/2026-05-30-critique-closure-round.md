# 2026-05-30 — critique round closure (issues #9-#20, sibling 3.7.3, dashboard 4.17)

## Motivation

GitHub issues #9-#20 (all labeled `critique`) were filed 2026-05-28 against
the autostop, NCBI quota, openapi `/v1/ready`, and audit-log subsystems.
Triage on 2026-05-30 confirmed:

* Most fixes had already landed in prior waves
  (see `2026-05-29-autostop-critique-wave-a.md`, `wave-b.md`,
  `openapi-critique-fixes.md`, `critique-round1-and-round2.md`) but the
  issues were never closed.
* Three items still needed work:
  * **#9.6** `mark_auto_stop_event` TOCTOU residual window was not
    explicitly documented in the helper docstrings.
  * **#20.4** the openapi upstream-codes contract test only compared
    dashboard ↔ SPA; it did not gate against the sibling's emitted code
    set, so a sibling-only addition would still ship dashboard-side
    tests green while real submits lost remediation hints.
  * **#20.11** the prior change note for the openapi critique round did
    not record the cross-repo rollout order, which is what allowed the
    P0 #1 dangling pin (`elb-openapi:4.16` referenced but never built)
    to slip through.
* One blocker: **#20 P0 #1** — `IMAGE_TAGS["elb-openapi"] = "4.16"` pointed
  at an image that did not exist in ACR. Already mitigated by the
  2026-05-30 rollback (`2026-05-30-openapi-pin-p0-rollback.md`) which
  pinned back to `4.14`.
* Four sibling-owned items: **#20 P1 #2, P1 #3, P3 #8, P3 #9** in
  `elastic-blast-azure/docker-openapi`. Fixed in sibling commit
  `3382a10` (`master`).

This note records the closure of all 13 issues: code fixes shipped here,
sibling 3.7.3 built + pushed to ACR as `elb-openapi:4.17`, and dashboard
re-pinned to `4.17`.

## User-facing change

* **`/v1/ready` (sibling `elb-openapi` 3.7.3)** — anonymous bucket is now
  keyed on the real downstream IP (X-Forwarded-For first hop) so one
  noisy laptop behind the in-pod nginx + Container Apps ingress can no
  longer DoS every other anonymous caller; the per-key rate-bucket dict
  is LRU-bounded so long-running pods do not accumulate unbounded SHA-256
  keys; the autoscaler-aware probe parses `Name:` / `Pool Name:` field
  lines and exact-matches `ELB_OPENAPI_WORKLOAD_POOL_NAME` (no more
  false-positive substring match like `blast` ⊂ `warmupblast`,
  `pool` ⊂ `systempool`).
* **`mark_auto_stop_event` / `extend_auto_stop_preference` docstrings**
  now document the residual TOCTOU window (read-modify-write between the
  freshly-read row and `save_auto_stop_preference` is not atomic) and
  point to the deferred ETag-based optimistic concurrency follow-up.
* **`api/tests/test_openapi_upstream_codes_contract.py`** adds a
  `KNOWN_SIBLING_NESTED_CODES` parity assertion that mirrors the codes
  the sibling `v1_ready` handler emits; sibling-only drift now fails the
  dashboard suite.
* **Dashboard pin** moves from `4.14` (rollback state) to `4.17`
  (sibling 3.7.3). End-to-end behaviour for callers picking up the new
  image: identical to what `4.16` was supposed to ship plus the four
  P1/P3 sibling fixes from `3.7.3`.

## API / IaC diff summary

| Surface | Change |
| --- | --- |
| sibling `docker-openapi/app/main.py` (commit `3382a10`) | `VERSION = 3.7.3`; `_anonymous_client_ip` helper (X-Forwarded-For / X-Real-IP / client.host); `_READY_RATE_BUCKETS` switched to `OrderedDict` + LRU eviction bounded by `READY_RATE_BUCKETS_MAX` (env `ELB_OPENAPI_READY_RATE_BUCKETS_MAX`, default 4096); `_autoscaler_status_mentions_pool` exact-match parser; in-line probe-budget note. |
| sibling `docker-openapi/tests/test_ready.py` | +4 tests (`test_anonymous_client_ip_*`, `test_ready_token_bucket_lru_touches_on_reuse`, `test_autoscaler_status_mentions_pool_is_exact_match`); existing GC-on-empty test rewritten as LRU eviction assertion. |
| `api/services/auto_stop.py` | `mark_auto_stop_event` and `extend_auto_stop_preference` docstrings call out the residual TOCTOU window (critique #9.6). No code change. |
| `api/tests/test_openapi_upstream_codes_contract.py` | New `KNOWN_SIBLING_NESTED_CODES` constant + `test_dashboard_codes_match_known_sibling_codes` (critique #20.4). |
| `docs/features_change/2026-05/2026-05-29-openapi-critique-fixes.md` | New "Rollout order" section explaining the safe sibling-build-pin sequence (critique #20.11). |
| `api/services/image_tags.py` | `elb-openapi` pin `4.14` → `4.17`; comment block refreshed to record `4.17` ↔ sibling `3.7.3` mapping and point at the rollout-order doc. |

No IaC changes. No new dependencies. Storage
`publicNetworkAccess: Disabled` posture unchanged.

## Validation evidence

```
$ cd ~/dev/elastic-blast-azure/docker-openapi
$ /tmp/eba-venv/bin/python -m pytest tests/ -q
18 passed, 39 warnings in 1.94s

$ az acr repository show-tags --name acrelbdashboard3abp67bppe \
    --repository elb-openapi -o tsv
4.14
4.17

$ cd ~/dev/elb-dashboard
$ uv run pytest -q api/tests/test_openapi_upstream_codes_contract.py api/tests/test_auto_stop.py
16 passed

$ uv run ruff check api
All checks passed!
```

Full backend suite + frontend build run separately (see commit). The
`elb-openapi:4.17` image carries digest
`sha256:248ab95ebcfa2037f8741217b6013c938683b25642a99358bbab48ffeec71e92`
(ACR build run `de1r`, 2m59s, 2026-05-30 02:44 UTC).

## Issue closure mapping

| Issue | Resolution |
| --- | --- |
| #9 (AKS auto-stop third-round critique, 10 sub-items) | sub-items 1, 2, 3, 4, 5, 7, 8, 9, 10 already landed in `2026-05-29-autostop-critique-wave-*.md`; sub-item 6 docstring-documented here; verified by existing `api/tests/test_auto_stop*.py`, `api/tests/test_aks_autostop_route.py`. |
| #10 NCBI per-caller bucket collapses dev-bypass / anonymous | already fixed: `_caller_bucket_key` namespaces dev-bypass by `upn`, raises 401 on empty oid (`api/routes/ncbi.py`); covered by `test_caller_bucket_key_namespaces_dev_bypass_by_upn` + `test_caller_bucket_key_rejects_empty_oid`. |
| #11 `_CALLER_BUCKETS` unbounded dict | already fixed: `OrderedDict` + LRU eviction bounded by `_CALLER_BUCKETS_MAX_KEYS=4096`; covered by `test_caller_bucket_lru_eviction`. |
| #12 `evaluate_idle_clusters` rollback stale snapshot | already fixed: rollback re-fetches the latest persisted row before write (`api/tasks/azure/idle_autostop.py:333`). |
| #13 NCBI per-caller quota charged before shared bucket | already fixed: `_check_caller_quota` returns bucket key + `_refund_caller_quota` drops the slot when the shared bucket throttles (`api/routes/ncbi.py`); covered by `test_caller_quota_refund_on_shared_bucket_throttle`. |
| #14 `_save_file` orphan `.lock` files | already fixed: file backend now uses a process-local `threading.Lock` (`api/services/auto_stop.py:422`, `auto_warmup.py:343`); covered by `test_auto_stop_no_lock_file_left` + `test_auto_warmup_no_lock_file_left`. |
| #15 `_batch_power_states` silent RBAC degradation | already fixed: WARNING log + `summary["errors"]` increment + `power_state_failed_rgs` surface (`api/tasks/azure/idle_autostop.py:80,120,245`); covered by `test_auto_stop_task.py:309`. |
| #16 `_audit_session` interrupted-event escalation | already fixed: cascading audit failure logs ERROR line (`api/routes/settings/vnet_peering.py:153`). |
| #17 `AutoStopPanel` countdown stale after manual stop | already fixed: `clusterIsRunning` true→false invalidates the status query (`web/src/components/ClusterItem/AutoStopPanel.tsx:114,146`). |
| #18 `/autostop/status` per-process cache incoherent | already fixed: L2 ops Redis cache invalidates across workers (`api/routes/aks/autostop.py:203`); covered by `api/tests/test_aks_autostop_route.py` Redis tests. |
| #19 `_CALLER_BUCKETS_GUARD` lazy-init race + `deque` | already fixed: module-level `threading.Lock()`, `OrderedDict[str, deque[float]]`, `popleft()` for O(1) eviction (`api/routes/ncbi.py:50`); covered by `test_caller_bucket_guard_is_initialised_at_import`. |
| #20 openapi critique round (12 sub-items) | P0 #1 mitigated by `2026-05-30-openapi-pin-p0-rollback.md` then re-pinned here to `4.17`; P1 #2/#3, P3 #8/#9 fixed in sibling commit `3382a10` and shipped as `elb-openapi:4.17`; P1 #4 dashboard contract test extended here; P2 #5/#6/#7, P3 #12 already landed; P3 #10 (banner actionable button) tracked as separate follow-up (explicit in issue body); P3 #11 rollout-order section added to `2026-05-29-openapi-critique-fixes.md`. |

## Deferred follow-up

* **ETag-based optimistic concurrency** for every preference table
  (`auto_stop` + `auto_warmup` + future additions) — would eliminate the
  residual TOCTOU window in `mark_auto_stop_event` /
  `extend_auto_stop_preference`. Charter-level decision, needs
  maintainer review. Filed as a follow-up tracking note.
* **PLS transition banner actionable button** (critique #20 P3 #10) —
  the issue body itself classifies this as a separate ticket; the
  banner currently links to the deploy panel without a one-click
  remediation button.

## Cross-references

* Issues: [#9](https://github.com/dotnetpower/elb-dashboard/issues/9),
  [#10](https://github.com/dotnetpower/elb-dashboard/issues/10),
  [#11](https://github.com/dotnetpower/elb-dashboard/issues/11),
  [#12](https://github.com/dotnetpower/elb-dashboard/issues/12),
  [#13](https://github.com/dotnetpower/elb-dashboard/issues/13),
  [#14](https://github.com/dotnetpower/elb-dashboard/issues/14),
  [#15](https://github.com/dotnetpower/elb-dashboard/issues/15),
  [#16](https://github.com/dotnetpower/elb-dashboard/issues/16),
  [#17](https://github.com/dotnetpower/elb-dashboard/issues/17),
  [#18](https://github.com/dotnetpower/elb-dashboard/issues/18),
  [#19](https://github.com/dotnetpower/elb-dashboard/issues/19),
  [#20](https://github.com/dotnetpower/elb-dashboard/issues/20).
* Sibling commit: `dotnetpower/elastic-blast-azure@3382a10`
  ("fix(openapi): /v1/ready critique round (P1 #2/#3, P3 #8/#9) — bump 3.7.3").
* Previous waves: `2026-05-29-autostop-critique-wave-a.md`,
  `2026-05-29-autostop-critique-wave-b.md`,
  `2026-05-29-openapi-critique-fixes.md`,
  `2026-05-29-critique-round1-and-round2.md`.
* P0 mitigation: `2026-05-30-openapi-pin-p0-rollback.md`.
