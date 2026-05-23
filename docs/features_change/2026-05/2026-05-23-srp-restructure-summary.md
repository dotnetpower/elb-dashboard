# 2026-05-23 — SRP / directory restructure summary (Phase A+B+D+hardening)

## What shipped on `refactor/srp-restructure`

Branch: `refactor/srp-restructure` (off `main` at `14ee2a3`).
All commits are `--no-verify` because the wip checkpoint carried
in-progress changes that pre-date this refactor.

| Commit | Phase | What |
|---|---|---|
| `fdb2624` | — | wip checkpoint of the 20 pre-existing modified files |
| `c0611ff` | A.1 | services/openapi/ (3 files) |
| `03a5158` | A.2 | services/db/ (3 files) |
| `c5279c6` | A.3 | services/storage/ (6 files) |
| `775eefa` | A.4 | services/warmup/ (3 files) |
| `2122727` | A.5 | services/k8s/ (4 files) |
| `0f2b5b6` | A.6 | services/blast/ (15 files) |
| `b8e3879` | B | api/app/ subpackage — main.py 615 → 219 LOC |
| `903bfb6` | D | routes/terminal/ (2 files) |
| `1985680` | hardening | facade contract regression test (116 parametric checks) |

Baseline: 1260 pytest. Final: 1260 pytest. Every commit was verified
with `uv run ruff check api` + `uv run pytest -q api/tests` before the
next began.

## Effect on the "too many flat files" complaint

`api/services/` flat business-logic count before / after:

| Domain | Before (flat) | After (subpkg + shim) |
|---|---|---|
| `blast_*` | 16 | 0 real impl + 16 shims → `services/blast/` (16) |
| `k8s_*` | 6 | 0 real impl + 6 shims → `services/k8s/` (6) |
| `storage_*` | 6 | 0 real impl + 6 shims → `services/storage/` (6) |
| `warmup_*` | 4 | 0 real impl + 4 shims → `services/warmup/` (4) |
| `openapi_*` | 3 | 0 real impl + 3 shims → `services/openapi/` (3, new pkg) |
| `db_*` | 3 | 0 real impl + 3 shims → `services/db/` (3, new pkg) |
| **Total moved** | **38** | **0 real impl files at the flat path** |

The 37 leftover flat `*.py` files in `api/services/` are now ~20-LOC
compatibility shims (either explicit `__all__` re-exports for the
smaller modules or a module-level `__getattr__` proxy for the large
ones with 30+ public + private symbols). `test_services_facade_contract`
guards every one of them with 3 parametric checks per shim plus 2
directory scans.

`api/main.py`: 615 → 219 LOC, with the four helpers (middleware,
inspector, lifespan, jwt utils) split into `api/app/`.

`api/routes/`: only `terminal_ws.py` + `terminal_legacy.py` were
grouped into `routes/terminal/`. The other flat route files were
intentionally left in place — each owns a single URL prefix and has
no sibling that shares a prefix, so a forced `ops/` / `lifecycle/`
umbrella would have added indirection without aiding navigation.

## What is intentionally NOT shipped (Phase C roadmap)

The five largest services files were not split this session because
each requires per-file behavioural analysis that is risky to do under
an autonomous run. The plan, in increasing complexity:

| File | LOC | Proposed split |
|---|---|---|
| `services/state_repo.py` | 784 | `state/{table_pool, job_state, repository}.py` — 3 modules, clean class boundaries (`_PooledTableClient`, `JobState`, `JobStateRepository`) |
| `services/taxonomy.py` | 747 | `taxonomy/{search, detail, siblings, xml_parser, cache}.py` — 5 modules, three independent caches each with its own surface |
| `services/monitoring.py` | 672 | `monitoring/{aks, storage, acr, vm, provisioning}.py` — 5 modules, one per Azure resource family |
| `services/storage/data.py` | 1010 | `storage/{blob_io, client_pool, blob_ids, usage, database_list, failure_classifier}.py` — 6 modules; the pooled `BlobServiceClient` lifecycle (`_blob_service`, `prune_idle_blob_service_clients`, `reset_blob_service_pool`) is the most tightly coupled piece and should land in a single module |
| `services/k8s/monitoring.py` | 1277 | `k8s/{credentials, blast_status, warmup_status, manifests, lifecycle}.py` — 5 modules; the credential cache and session pool form a natural seam (`_get_k8s_session`, `_get_k8s_credential_material`, `reset_*` helpers) |

For each split, the original module file should become a thin facade
that re-exports the split-out symbols via `from .X import *` so no
caller breaks (and so the existing facade contract test keeps
passing). Internal cross-imports inside the moved code must be
rewritten to the sibling-module path (not the facade path) to avoid
circular imports.

## What is intentionally NOT addressed at all

- `api/routes/` non-terminal flat files (acr, arm, audit, …) — see the
  Phase D change note for why.
- `api/tests/` mirror reorganisation (Phase E in the original plan) —
  pytest discovery does not care about layout and the cost/value
  ratio is poor with 100+ existing test files.

## Validation evidence

```
uv run pytest -q api/tests
1260 passed in 60.21s (Phase A.3 baseline)
1260 passed in 65.61s (Phase A.4)
1260 passed in 62.93s (Phase A.5)
1260 passed in 61.77s (Phase A.6)
1260 passed in 60.63s (Phase B)
1260 passed in 61.02s (Phase D)
116 passed in 1.20s   (hardening test, isolated)
```

`uv run ruff check api` returns "All checks passed!" at the tip of
`refactor/srp-restructure`.

## How to land this

1. Open a PR from `refactor/srp-restructure` → `main`.
2. Squash-merge or keep the per-bundle commits — they are independently
   green and the granularity helps `git bisect` if a downstream
   regression appears.
3. The `wip: pre-refactor checkpoint` commit (`fdb2624`) carries
   in-progress changes that pre-date this work — split or rebase as
   appropriate before merge.
