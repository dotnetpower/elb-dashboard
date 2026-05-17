# Taxonomy detail (efetch) + Wikipedia image proxy

## Motivation

The taxonomy section we shipped on 2026-05-17 returned only what NCBI
esummary provides (taxid, scientific_name, common_name, rank, division).
The fields the UI needs to render Variant B of the new modal — lineage
string, full `LineageEx` rank ladder, synonyms, authority, parent
taxid, genetic codes, an organism thumbnail — were not available, so
the right-hand "preview" pane would have been mostly empty.

This change adds the missing fields and an optional image fallback
while keeping the list endpoint as cheap as it was.

## User-facing change

- The Taxonomy modal can now render a rich preview pane for the
  selected candidate (lineage, ranks, synonyms, misspellings, authority,
  parent taxon link, genetic codes, update date) and a Wikipedia
  thumbnail when one is available.
- Search/list behaviour is unchanged. Detail and image are fetched
  lazily *only* when a user selects a candidate, so an idle search does
  not increase NCBI/Wikipedia load.
- When Wikipedia has no page for the scientific name, the UI shows a
  default organism icon. The endpoint never 5xx's on a missing page.

## API surface added

| Method | Path | Notes |
| ------ | ---- | ----- |
| GET | `/api/blast/taxonomy/detail/{taxid}` | path-validated `int >= 1`. Returns the rich payload (see `TaxonomyDetail` in `web/src/api/blast.ts`). 24 h in-process cache. Maps upstream failure to 503 with a `taxonomy_lookup_unavailable` code. |
| GET | `/api/blast/taxonomy/image?name=<scientific_name>` | name allowlist (letters incl. accents, digits, space, `-.()×`); 24 h cache; never raises on upstream failure (returns `image_url=null`). Bad name returns 422 with `taxonomy_image_invalid_name`. |

`/api/blast/taxonomy/search` shape gains an optional `division` field
on each result (esummary already returns it, we just exposed it).

## Backend modules

- New: [api/services/taxonomy_image.py](../../../api/services/taxonomy_image.py).
- Modified: [api/services/taxonomy.py](../../../api/services/taxonomy.py).
- Modified: [api/routes/stubs.py](../../../api/routes/stubs.py)
  (routes only; auth dependency unchanged).
- New dep: `defusedxml==0.7.1` in [pyproject.toml](../../../pyproject.toml).

## Hardening checklist applied

| Concern | Mitigation |
| ------- | ---------- |
| XXE / billion-laughs on the efetch XML | `defusedxml.ElementTree`; `DefusedXmlException` mapped to `TaxonomySearchUnavailable` |
| Runaway upstream body | Streaming reads with hard caps (`MAX_EFETCH_BYTES=512 KiB`, `MAX_BODY_BYTES=64 KiB`); the loop aborts as soon as the cap is exceeded |
| SSRF via the image name | Strict allowlist regex (`_NAME_PATTERN`), `urllib.parse.quote` with `safe=""`, hardcoded `WIKIPEDIA_BASE_URL`, single path segment; returned thumbnail URL must start with `https://upload.wikimedia.org/` |
| Path-injection via taxid | `Path(..., ge=1, le=10_000_000_000)` on the route plus `_normalise_taxid` on the service |
| Cache poisoning across keys | Image cache key is `.lower()` of the normalised name; detail cache is keyed by taxid `int` |
| Cache unbounded growth | FIFO eviction at `MAX_DETAIL_CACHE_ENTRIES` / `MAX_CACHE_ENTRIES` (1024 each) |
| Logging that leaks sensitive data | Only logs class name / status code / sanitised exception messages |
| Test isolation | `clear_taxonomy_detail_cache()` / `clear_taxonomy_image_cache()` called at the top of every relevant test |
| Auth bypass | Both new routes use the same `require_caller` dependency as `taxonomy/search` |

## Validation evidence

```bash
$ uv run pytest -q api/tests
517 passed in 24.93s

$ uv run pytest -q api/tests/test_taxonomy_detail.py \
                 api/tests/test_taxonomy_image.py \
                 api/tests/test_taxonomy_search.py
40 passed in 1.70s

$ uv run ruff check api/services/taxonomy.py \
                    api/services/taxonomy_image.py \
                    api/tests/test_taxonomy_detail.py \
                    api/tests/test_taxonomy_image.py
All checks passed!

$ (cd web && npm run build)
✓ built in 6.57s
```

Live smoke against real NCBI + Wikipedia (executed locally):

- `fetch_taxonomy_detail(9606)` returned 30 `lineage_ex` rows,
  authority `"Homo sapiens Linnaeus, 1758"`, parent_taxid `9605`,
  genetic_code `Standard` / mito `Vertebrate Mitochondrial`,
  misspellings `["Home sapiens", "Homo sampiens", "Homo sapeins", …]`.
- `fetch_taxonomy_detail(562)` (E. coli) returned the bacterial lineage
  starting at `cellular organisms → Bacteria → Pseudomonadati → …`.
- `fetch_taxonomy_image("Homo sapiens")` returned the
  `upload.wikimedia.org/.../330px-Akha_cropped_hires.JPG` thumbnail and
  the `en.wikipedia.org/wiki/Human` page URL.
- `fetch_taxonomy_image("Notarealorganism")` returned
  `image_url=null, page_url=null` without raising.

## Out of scope (deferred)

- Wiring Variant B of the modal in [web/src/pages/blastSubmit/TaxonomyFilterSection.tsx](../../../web/src/pages/blastSubmit/TaxonomyFilterSection.tsx). The typed client (`blastApi.getTaxonomyDetail`, `blastApi.getTaxonomyImage`) is in place; the component refactor will be a separate change so the API surface can be reviewed independently.
- A persistent (Storage/Table-backed) cache. The current 24 h in-process cache is enough for the single-replica `api` sidecar; revisit only if we move to multi-replica.
