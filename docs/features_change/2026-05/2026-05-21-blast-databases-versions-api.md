# 2026-05-21 — `/api/blast/databases/versions` 구현

## Motivation
SPA "DB Versions" 탭(`web/src/pages/tools/tabs/DbVersionsTab.tsx`)이
`dbVersionApi.list()` 로 `/api/blast/databases/versions` 를 호출하고 있었지만,
백엔드 라우트는 stub 이라 항상 `{"versions": {}, "degraded": true}` 만
반환해서 표가 영구적으로 비어 있었다. 다운로드한 BLAST DB 목록/버전을
확인할 방법이 사실상 없는 상태였다.

## User-facing change
- Tools → DB Versions 탭이 실제로 다운로드된 DB 행을 표시한다.
- 각 행은 DB 이름, type(`nucl`/`prot`), source(`ncbi`/`custom`), source_version,
  downloaded_at(=`created_at`)을 보여준다.
- 기존 `/api/blast/databases`(상세 페이로드)는 변경 없음 — DB Versions 탭은
  더 가벼운 projection 을 받는다.

## API diff
`GET /api/blast/databases/versions?subscription_id=&storage_account=&resource_group=`

- 이전: `{"versions": {}, "degraded": true,
  "degraded_reason": "blast_db_listing_not_yet_implemented"}` (stub)
- 신규: `{"versions": DbVersionMeta[], "total": int}`
  - `DbVersionMeta`: `db_name`, `source`, `source_version`, `created_at`,
    `_last_modified`, optional `db_type`, `title`, `version_tag`.
  - storage_account/resource_group 누락 시 `{"versions": [], "total": 0}`.
  - Storage data-plane 실패 시 기존 `classify_storage_failure(...)` 와 같은
    `degraded` 마커 첨부.

데이터 소스는 새 SDK 호출이 아니라 기존 `list_databases()`
(`api/services/storage_data.py`). `.njs` + `{db}-metadata.json` 에서 이미
파싱하던 필드를 SPA 가 기대하는 `DbVersionMeta` 모양으로 reshape 만 한다.

## Validation
- `uv run pytest -q api/tests/test_blast_databases_versions.py` — 3 passed
  (missing-params empty, happy path projection + sort + optional-field
  omission, storage failure -> degraded).
- `uv run pytest -q api/tests/test_blast_databases_warmup_plan.py
  api/tests/test_blast_databases_versions.py api/tests/test_route_contracts.py`
  — 13 passed.
- `uv run ruff check api/routes/blast/databases.py
  api/tests/test_blast_databases_versions.py` — All checks passed.

## Files
- `api/routes/blast/databases.py` — `blast_databases_versions` stub -> real
  projection over `list_databases()`.
- `api/tests/test_blast_databases_versions.py` — new regression suite.
