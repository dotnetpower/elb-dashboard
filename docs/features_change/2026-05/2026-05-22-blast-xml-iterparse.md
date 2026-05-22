# BLAST XML parser — incremental walk via iterparse

## Motivation
`parse_blast_xml` called `ET.fromstring(content)` and walked the full DOM
to extract per-HSP rows. For the 20 MiB XML cap that route handlers
enforce, the resident DOM blew up to ~100-200 MiB while the function ran;
multiple concurrent analytics calls summed to GB-scale worker RSS spikes.

## User-facing change
None. Same row schema, same field coercions, same namespace handling
(verified by `test_parse_blast_xml_namespaced`).

## API / IaC diff
* `api/services/blast_results_parser.py`
  * `parse_blast_xml` rewritten as a `defusedxml.iterparse` state machine
    that walks `start`/`end` events, captures Iteration-level query
    metadata, fans out one row per `<Hsp>`, and `elem.clear()`-s each
    `<Hit>` and `<Iteration>` subtree as it closes so the parser's
    resident DOM is bounded by one Hit subtree.
  * New private helper `_build_hit_row(...)` keeps the per-HSP row
    construction unchanged but factored out so the walker stays readable.
* No new dependency; `defusedxml.ElementTree.iterparse` is part of the
  existing `defusedxml` pin.

## Validation
* `uv run pytest -q api/tests/test_blast_results_parser.py
  api/tests/test_blast_results_routes.py` — 47 passed (XML, namespaced
  XML, route export and aggregate paths all green).
* `uv run ruff check api/services/blast_results_parser.py` — clean.
