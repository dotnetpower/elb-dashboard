# 2026-05-21 — BLAST REST API: database catalogue endpoints

## Motivation

The dashboard's `/docs` page renders the elb-openapi service spec (the
public BLAST REST API hosted on AKS). Researchers can submit jobs and
inspect clusters from there, but the public spec exposes **no way to
discover which BLAST databases are prepared** on the workspace Storage
account or to read each database's version. They had to open a separate
dashboard view to check, breaking the "one documented API" promise.

Per user direction:

> openapi 에는 없어 database 가.. 그래서 여기 추가하자는거야

We add the missing database endpoints to the `elb-openapi` service so
they appear in `/docs` alongside `/v1/health`, `/v1/cluster`, and
`/v1/jobs`.

## User-facing change

The elb-openapi service (`elastic-blast-azure/docker-openapi`) gains a
new tag and two endpoints:

- `GET /v1/databases` — list databases prepared under the workspace
  `blast-db` container. Returns
  `{ "databases": [{ "name": "core_nt" }, …], "count": N, "container": "blast-db" }`.
- `GET /v1/databases/{db_name}` — return molecule type, effective
  version, raw metadata version, dbtype, sequence counts, and the
  description for one database. 404 when the database has no
  `*-nucl-metadata.json` or `*-prot-metadata.json` file.

Both endpoints sit under the existing `v1` `APIRouter` and share the
`require_api_token` dependency, so they enforce the same
`X-ELB-API-Token` gate as `/v1/jobs`.

Once the rebuilt image is deployed, the dashboard's API Reference page
will surface these endpoints automatically because the SPA renders
whatever `/openapi.json` the elb-openapi pod returns — no SPA code
change is needed for them to appear.

## API / IaC diff summary

### `elastic-blast-azure/docker-openapi/app/main.py` (sibling repo)

- `tags_metadata` += `{"name": "Databases", "description": "..."}`.
- `VERSION` bumped `3.3.0` → `3.4.0`.
- New helpers:
  - `_storage_oauth_token()` — `DefaultAzureCredential` → short-lived
    Storage data-plane bearer.
  - `_list_blast_database_names(container="blast-db")` — pages through
    the Azure Blob REST API with `delimiter=/`, returning unique sorted
    top-level prefixes. 404 on the container maps to `FileNotFoundError`;
    other HTTP errors propagate.
  - `_database_metadata(db_name)` — tries `*-nucl-metadata.json` then
    `*-prot-metadata.json` via the existing `azcopy cp` pattern, returns
    the normalised metadata dict or `None`.
- New Pydantic models: `DatabaseListItem`, `DatabaseList`,
  `DatabaseMetadata`.
- New endpoints (placed between `/v1/cluster` and the Jobs section):
  - `GET /v1/databases` → `DatabaseList`
  - `GET /v1/databases/{db_name}` → `DatabaseMetadata`

### `elb-dashboard/api/services/image_tags.py`

- `IMAGE_TAGS["elb-openapi"]` bumped `4.10` → `4.11`. The dashboard's
  existing image-update panel will surface this as an actionable
  "rebuild + redeploy" prompt once the sibling repo's image is built.

### No SPA change

The `web/src/pages/ApiReference.tsx` page already renders the
elb-openapi spec verbatim. No file changes were needed to display the
new endpoints — they appear automatically after the rebuilt image is
deployed to AKS.

## Validation evidence

1. **Syntax**: `python3 -c "import ast; ast.parse(open('app/main.py').read())"` → `parse OK`.
2. **OpenAPI schema**: imported the app and dumped `app.openapi()` ([direct quote of run output]):

   ```text
   database paths: ['/v1/databases', '/v1/databases/{db_name}']
     GET /v1/databases tags=['Databases'] summary=List prepared BLAST databases
     GET /v1/databases/{db_name} tags=['Databases'] summary=Get BLAST database metadata
   tag names: ['System', 'Cluster', 'Databases', 'Jobs']
   ```

3. **`_list_blast_database_names` unit smoke**: replaced
   `requests.get` with a fake returning a two-page response (one
   `NextMarker` then empty). Got `['core_nt', 'nr', 'swissprot']`
   (pagination + dedup + sort). 404 path returned `FileNotFoundError`.
4. **Dashboard regression**: `uv run pytest -q api/tests/test_acr_build_task.py api/tests/test_blast_databases_versions.py api/tests/test_openapi_deployment.py api/tests/test_openapi_proxy_route.py api/tests/test_openapi_task.py` → **18 passed**.

## Build + deploy (this session, autonomous)

- Sibling repo commit: `3ed67778` on `feat/parallel-submit-prep` (local
  only — **not pushed** to GitHub).
- ACR build: `az acr build --registry elbacr01 --image elb-openapi:4.11
  --file docker-openapi/Dockerfile docker-openapi` → ACR run `de1r`
  succeeded in 2m53s, digest
  `sha256:bbf5aef001d0fddb20608041d9882c4e21308d111f5b7c0f1822ca6ac4d270f8`.
- AKS rollout: `kubectl set image deployment/elb-openapi -n default
  openapi=elbacr01.azurecr.io/elb-openapi:4.11` on cluster
  `elb-cluster` (rg `rg-elb-01`, Korea Central). New pod
  `elb-openapi-5574f8cd76-md928` `Running`, old pod `4.10` terminated.
- Live spec verification (`curl http://20.249.48.153/openapi.json`):
  `version 3.4.0`, **14** endpoints, **4** tags (`System`, `Cluster`,
  `Databases`, `Jobs`), `/v1/databases` and `/v1/databases/{db_name}`
  both present.

## Bugfix follow-up — `/v1/databases` filter (same day)

### Problem

Calling the deployed endpoint returned **14** entries instead of the
expected 5 real databases. Extras: `1shards/`, `2shards/`, `3shards/`,
`4shards/`, `5shards/`, `6shards/`, `8shards/`, `10shards/`,
`metadata/`. Reason: `_list_blast_database_names()` listed every
top-level prefix under `blast-db` with `delimiter=/`, but that
container also holds prepare-db shard layouts written by the
dashboard's `ensure_shard_sets()` (`{N}shards/{db}_shard_{NN}/…`), the
oracle staging directory (`metadata/oracles/{db}/…`), the custom-db
staging area (`custom-db-build/`), and `.staging/` artifacts. None of
those are user-actionable databases.

### Fix

`docker-openapi/app/main.py` adds `_NON_DATABASE_PREFIXES` (frozen set
of `metadata`, `custom-db-build`, `.staging`, `custom_db`) and
`_SHARD_PREFIX_RE = re.compile(r"^\d+shards$")`, exposed via
`_is_database_prefix(name)`. The BlobPrefix loop now appends only when
the helper returns `True`, mirroring the same skip rules used by the
dashboard's `api/services/storage_data.py::list_databases`.

`VERSION` bumped `3.4.0` → `3.4.1`; rebuild target
`elbacr01.azurecr.io/elb-openapi:4.12` (overrides the polluted 4.11).
`elb-dashboard/api/services/image_tags.py::IMAGE_TAGS["elb-openapi"]`
bumped `4.11` → `4.12` to match.

### Validation

1. **Helper unit smoke** (in sibling repo
   `docker-openapi/app/main.py` via `ELB_OPENAPI_ALLOW_UNAUTHENTICATED=1
   python3.11`):

   ```text
   '10shards' -> False     '1shards' -> False     '5shards' -> False
   '8shards' -> False      'metadata' -> False    'custom-db-build' -> False
   '.staging' -> False     'custom_db' -> False   'core_nt' -> True
   '16S_ribosomal_RNA' -> True   'elb_compare_tiny' -> True
   'ITS_RefSeq_Fungi' -> True    '' -> False     '100shards' -> False
   '11shards' -> False     'shards' -> True       'dbshards' -> True
   ```

2. **ACR build**: `az acr build --image elb-openapi:4.12 …` → run `de1s`
   succeeded in 2m53s, digest
   `sha256:6ffd57af5dd4c3f1da7a7f9c9b84eb829bdd6ac28cd91a0b3693bd0e0f1b408d`.

3. **AKS rollout**: `kubectl set image deployment/elb-openapi -n
   default openapi=elbacr01.azurecr.io/elb-openapi:4.12` →
   `successfully rolled out`. New pod `elb-openapi-75dcd9c947-4fs6c`
   `Running`, old `4.11` pod terminated.

4. **Live spec**: `curl http://20.249.48.153/openapi.json` returns
   `version: 3.4.1`.

5. **Live endpoint** (`curl -H 'X-ELB-API-Token: …'
   http://20.249.48.153/v1/databases`):

   ```json
   {
     "databases": [
       {"name": "16S_ribosomal_RNA"},
       {"name": "18S_fungal_sequences"},
       {"name": "ITS_RefSeq_Fungi"},
       {"name": "core_nt"},
       {"name": "elb_compare_tiny"}
     ],
     "count": 5,
     "container": "blast-db"
   }
   ```

   Pollution gone — only the five real databases remain.

### Known limitation

Custom databases live one level deeper under `custom_db/{name}/…`. The
current fix skips the `custom_db/` top-level prefix entirely, so they
are still invisible. A follow-up can recurse one level into
`custom_db/` to surface them; out of scope for this fix because the
reported bug was extra noise, not missing entries.

## Follow-up

- Push the sibling repo commits (`3ed67778` add + `7f79101c` fix) to
  GitHub when ready — the dashboard's ACR Build Task still pulls from
  `https://github.com/dotnetpower/elastic-blast-azure.git`, so any
  future rebuild via the dashboard panel would currently miss these
  changes.
- Rollback recipe: `kubectl rollout undo deployment/elb-openapi -n
  default` reverts one step (4.12 → 4.11). Reverting further to 4.10
  requires `kubectl set image … =elbacr01.azurecr.io/elb-openapi:4.10`.

## Cross-repo coordination

Per charter §13 "Cross-repo consistency", the sibling repo
[`dotnetpower/elastic-blast-azure`](https://github.com/dotnetpower/elastic-blast-azure)
gets the substantive change (`docker-openapi/app/main.py`) and this
dashboard repo carries the matching `IMAGE_TAGS` bump in the same
review cycle.

## Schema 3.5.0 + HTTPS/ETag/TTL caching (same day, follow-up)

### Motivation

Two unrelated gaps surfaced after 4.12 went live:

1. **Sparse payload**: `GET /v1/databases/{db_name}` returned only 8
   fields (`name`, `molecule_type`, `version`, `metadata_version`,
   `dbtype`, `number_of_sequences`, `number_of_letters`, `description`)
   — the BLAST job-result header in the dashboard needs the NCBI
   snapshot date, last-updated timestamp, volume count, and byte totals,
   none of which were exposed. The source metadata JSON already has
   them (`last-updated`, `number-of-volumes`, `bytes-total`,
   `bytes-to-cache`, plus the date embedded in the file paths).
2. **Slow per-request fetch**: the existing helper used `azcopy cp`
   into `/tmp` per call (1–3 s subprocess + tmp file + auth roundtrip).
   For a dashboard panel that polls a handful of databases every few
   seconds that's noticeable. Metadata blobs change at most weekly when
   the NCBI snapshot rolls, so they're a perfect cache candidate.

Per user direction:

> 이 정보 처럼 아래 정보가 모두 포함되게 할수 있을까? 그리고 key 이름을
> 검토해서 제안해줄래?
> …
> 그래, 그런데 데이터를 찾는데 시간이 오래 걸리는건 아닌가? 똑똑하게
> 캐싱할수도 있어?
> …
> 모두 진행해

We expand the schema (with breaking key renames) and add an in-process
TTL + ETag cache.

### User-facing change

`GET /v1/databases/{db_name}` now returns 15 fields. Example:

```json
{
  "name": "core_nt",
  "container": "blast-db",
  "title": "Core nucleotide BLAST database",
  "dbtype": "Nucleotide",
  "molecule_type": "dna",
  "molecule_label": "mixed DNA",
  "snapshot": "2026-05-09-01-05-02",
  "last_updated": "2026-05-02T00:00:00",
  "number_of_sequences": 125619662,
  "number_of_letters": 1041443571674,
  "number_of_volumes": 88,
  "bytes_total": 292365689731,
  "bytes_to_cache": 263930372302,
  "metadata_schema_version": "1.1",
  "cached_at": "2026-05-21T15:11:00Z"
}
```

Breaking renames vs. the previous 4.12 payload:

| Old key            | New key                   | Notes                                                   |
| ------------------ | ------------------------- | ------------------------------------------------------- |
| `description`      | `title`                   | Source's `description` is a one-line title, not a blurb |
| `version`          | `snapshot`                | The NCBI snapshot timestamp (e.g. `2026-05-09-01-05-02`) |
| `metadata_version` | `metadata_schema_version` | The JSON schema version (e.g. `1.1`)                    |
| `molecule_type` was `"nucl"`/`"prot"` | `"dna"`/`"protein"` | Natural lowercase value |

`molecule_label` ("mixed DNA"/"protein") mirrors the dashboard's
`api/services/blast_db_metadata.py::_normalise_molecule_type` so UI
labels stay consistent whether the data came from elb-openapi or the
dashboard's own storage scanner.

`VERSION` in the OpenAPI spec bumps `3.4.1` → `3.5.0` to reflect the
breaking renames.

### API / IaC diff summary

#### `elastic-blast-azure/docker-openapi/app/main.py`

- `VERSION` bumped `3.4.1` → `3.5.0`.
- `_database_metadata` rewritten: drops `azcopy cp` + `/tmp` + `safe_exec`
  in favour of a direct HTTPS `GET` (`requests` + bearer token from
  `_storage_oauth_token` + `x-ms-version: 2020-04-08`). On TTL expiry
  sends `If-None-Match: <etag>` so unchanged blobs return `304` and skip
  the JSON parse.
- New helpers:
  - `_molecule_label(raw)` — maps `nucl`/`Nucleotide`/`dna` → `mixed DNA`,
    `prot`/`Protein`/`protein` → `protein`, anything else → identity.
  - `_fetch_blob_with_etag(url, etag, timeout)` — thin wrapper around
    `requests.get` returning `(status, parsed_json_or_none, new_etag)`,
    treating 304/404 as soft outcomes and propagating other ≥400.
  - `_normalise_metadata(db_name, raw, molecule_type_raw, *, container)`
    — projects the raw NCBI JSON into the 14 stable fields (snapshot
    extracted via `re.search(r"/(\d{4}-…)/")` on the first `files` URL).
  - `_project_with_cached_at(metadata, fetched_at)` — clones the cached
    payload and stamps `cached_at` in UTC ISO 8601.
- New module-level cache state (guarded by `Lock`):
  - `_db_metadata_cache: dict[(container, db), entry]` with entries
    `{"metadata", "etag", "suffix", "molecule_type_raw", "fetched_at"}`.
  - `_db_list_cache: dict[container, {"names", "fetched_at"}]` reused by
    `_list_blast_database_names` (toggleable via `use_cache=False`).
  - `_cache_evict_if_full` enforces a bounded LRU (default 128 entries,
    oldest `fetched_at` first).
- TTLs configured by env (charter §11 — no new dependencies):
  - `ELB_OPENAPI_DB_METADATA_TTL_SECONDS` (default `600`).
  - `ELB_OPENAPI_DB_LIST_TTL_SECONDS` (default `120`).
  - `ELB_OPENAPI_DB_CACHE_MAX_ENTRIES` (default `128`).
- `class DatabaseMetadata(BaseModel)` replaced with the new 15-field
  schema (see table above).

#### `elb-dashboard/api/services/image_tags.py`

- `IMAGE_TAGS["elb-openapi"]` bumped `4.12` → `4.13`.

### Validation evidence

1. **Syntax**: `python3 -c "import ast; ast.parse(open('docker-openapi/app/main.py').read())"` → `parse OK`.
2. **Helper unit smoke** (`ELB_OPENAPI_ALLOW_UNAUTHENTICATED=1 python3.11 …`):

   ```text
   label 'nucl'      -> 'mixed DNA'
   label 'prot'      -> 'protein'
   label 'Nucleotide'-> 'mixed DNA'
   label 'Protein'   -> 'protein'
   label 'dna'       -> 'mixed DNA'
   label 'protein'   -> 'protein'
   label ''          -> ''
   label 'weird'     -> 'weird'
   ```

3. **Normalisation against the real `core_nt` payload** (raw fields
   read with `az storage blob download` earlier in the session): the
   helper produced exactly the schema above, including
   `snapshot=2026-05-09-01-05-02` (extracted from the source `files[0]`
   URL) and `metadata_schema_version=1.1`.

4. **Cache + ETag round trip** (fake fetcher):

   ```text
   call1 fetch_count=1  snapshot=2026-05-09-01-05-02  molecule_type=dna
   call2 fetch_count=1  cached_at_changed?  False         # TTL hit, no fetch
   call3 fetch_count=2  etag_sent="v1"                    # TTL expired, 304
   call4 fetch_count=3  etag_sent="v1"                    # TTL expired, 200
   ```

   First call fetches; second is a TTL cache hit (no HTTP); third sends
   `If-None-Match: "v1"` and the fake returns 304 (cache refreshed
   in-place, no body); fourth sends `If-None-Match: "v1"` again and the
   fake returns 200 with `"v2"` (cache replaced).

5. **ACR build**: `az acr build --image elb-openapi:4.13 …` → run `de1t`
   succeeded in 2m51s, digest
   `sha256:2379d82bc6727ca3eda317c24518908cc94eb0723b93ab328fbc66e0d1c82cf9`.

### Deploy

`kubectl set image deployment/elb-openapi -n default openapi=elbacr01.azurecr.io/elb-openapi:4.13` →
`successfully rolled out`. New pod `elb-openapi-5496f745db-bfhqs`
`Running`, old pod terminated.

Live spec: `curl http://20.249.48.153/openapi.json` →
`version: 3.5.0`, paths include `/v1/databases` and `/v1/databases/{db_name}`.

Live shape on `core_nt` (direct quote of the response, formatted):

```json
{
  "name": "core_nt",
  "container": "blast-db",
  "title": "Core nucleotide BLAST database",
  "dbtype": "Nucleotide",
  "molecule_type": "dna",
  "molecule_label": "mixed DNA",
  "snapshot": "2026-05-09-01-05-02",
  "last_updated": "2026-05-02T00:00:00",
  "number_of_sequences": 125619662,
  "number_of_letters": 1041443571674,
  "number_of_volumes": 88,
  "bytes_total": 292365689731,
  "bytes_to_cache": 263930372302,
  "metadata_schema_version": "1.1",
  "cached_at": "2026-05-21T15:41:26Z"
}
```

Cache verification on the live endpoint (two consecutive `curl` calls
from the same shell, `time` output):

| Call | Latency | `cached_at` |
| ---- | ------- | ----------- |
| 1 (miss) | 0.367 s | `2026-05-21T15:41:26Z` |
| 2 (hit)  | 0.043 s | `2026-05-21T15:41:26Z` |

The second call is 8.5× faster and reports the same `cached_at`,
confirming the TTL path serves from cache without a Storage round-trip.

Rollback recipe (one step): `kubectl rollout undo deployment/elb-openapi -n default`.

### Cross-repo coordination

Sibling repo commit `80b005ae` on `master` is local-only (not pushed
yet). Combined with the earlier `7f79101c` filter fix it forms one
coherent change for the dashboard's image-update panel to consume on
its next rebuild.
