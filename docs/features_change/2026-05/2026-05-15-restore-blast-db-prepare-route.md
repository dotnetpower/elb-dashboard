# 2026-05-15 — Restore BLAST DB download via private-endpoint blob copy

## Motivation

The deployed Container App showed
*"`16S_ribosomal_RNA`: Resource not found. It may have been deleted or not
yet created."* whenever a user clicked **Get** on a BLAST database card on
the Dashboard's Storage panel. Root cause: the SPA calls
`POST /api/storage/prepare-db` and `GET /api/blast/databases/check-updates`,
but those routes were never ported from the legacy Function App. The
catch-all reverse proxy in [api/main.py](../../../api/main.py) forwarded
the unknown `/api/storage/*` path to the `frontend` sidecar, which 404'd,
and the SPA's generic 404 message leaked through.

## User-facing change

* **Get** button on every database card in the Dashboard → Storage panel
  now actually triggers a server-side copy from NCBI's public S3 bucket
  (`ncbi-blast-databases.s3.amazonaws.com`) into the workload Storage
  account's `blast-db` container.
* The "**N updates**" chip in the Storage card is now meaningful — the
  backend returns NCBI's real `latest-dir` snapshot id, and the SPA flags
  any locally-downloaded DB whose `source_version` differs.

## API / IaC diff

* **NEW** [api/routes/storage.py](../../../api/routes/storage.py) — adds
  `POST /api/storage/prepare-db`. Validates inputs (subscription id,
  resource group, storage account, db name), resolves NCBI's `latest-dir`,
  paginates the S3 list of `{snapshot}/{db}*` keys, then kicks off a
  daemon `ThreadPoolExecutor(20)` that issues `start_copy_from_url` per
  blob and writes a `{db}-metadata.json` summary blob at the end. Returns
  immediately with `{"async": true, "files_total": N, "source_version": …}`.
* **MODIFIED** [api/main.py](../../../api/main.py) — register
  `storage.router` BEFORE the `frontend_proxy` catch-all (charter §15
  routing-order rule).
* **MODIFIED** [api/routes/stubs.py](../../../api/routes/stubs.py) —
  `/api/blast/databases/check-updates` now returns NCBI's actual
  `latest_version` instead of a `degraded: true` stub. Lookup is a
  single 15 s `httpx.get` to the public bucket; cheap enough to keep
  in-process and not Celery-backed.
* **MODIFIED** [api/tests/test_smoke.py](../../../api/tests/test_smoke.py)
  — extends the auth-required parametrised test to cover
  `/api/storage/prepare-db`.

## Architecture invariants preserved

* Storage account stays `publicNetworkAccess: Disabled` at all times —
  the legacy Function App's "enable → copy → disable" toggle is **not**
  ported. The api sidecar reaches the storage account over the platform
  VNet's private endpoint via the shared MI; `start_copy_from_url` is a
  server-side fetch from Azure Storage out to NCBI S3 and is independent
  of the storage account's *inbound* public network setting.
  (cf. `.github/copilot-instructions.md` §9.)
* No SAS tokens are issued to the browser — the route accepts a JSON body
  and orchestrates server-side copies; the bytes never traverse the api
  sidecar. (cf. `api/services/storage_data.py` load-bearing comment.)
* New router is wired above `frontend_proxy.router` so the catch-all does
  not shadow it. (cf. `AGENTS.md` tripwire #7.)

## Validation evidence

* `uv run pytest -q api/tests` → **67 passed** (was 66; +1 covers the new
  route's auth gate).
* `uv run ruff check api/routes/storage.py` → 2 errors remain, both
  baseline `B008 Depends/Body in defaults` matching the rest of the
  codebase. No new categories introduced.
* TestClient smoke against the running app:
  * `POST /api/storage/prepare-db {"db_name": "invalid name with spaces", …}`
    → `400 invalid db_name: 'invalid name with spaces'` (validation works).
  * `POST /api/storage/prepare-db {}` →
    `400 subscription_id, storage_resource_group, account_name, db_name required`.
  * `GET /api/blast/databases/check-updates` → `200 {"latest_version":
    "2026-05-09-01-05-02", "updates_available": []}` (real NCBI lookup).
