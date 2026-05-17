# Web BLAST Low-Complexity Default

## Motivation

Current Web BLAST evidence for `core_nt` uses nucleotide low-complexity filtering.
The dashboard UI already appended `-dust yes` for normal `blastn` submissions,
but direct API submissions could omit that option while still receiving the
calibrated `core_nt` search-space default.

## User-Facing Change

`core_nt` Web-compatible defaults now carry `low_complexity_filter=true` through
the submit API. Users who disable low-complexity filtering keep that explicit
choice.

## API / IaC Diff Summary

- The BLAST submit option allow-list accepts `low_complexity_filter`.
- Web-compatible DB defaults set `low_complexity_filter=true` unless the caller
  explicitly opted out.
- Generated ElasticBLAST config renders `low_complexity_filter` as `-dust yes`
  or `-dust no`, without duplicating an explicit `-dust` in additional options.
  For Web-compatible `blastn` filtering, `-dust yes` also injects
  `-soft_masking false` unless the caller explicitly supplies `-soft_masking`.
- Frontend submit and preflight payloads now send the low-complexity setting.
- Added `scripts/dev/compare-blast-web-xml-outfmt6.py` so current Web BLAST XML
  RIDs can be compared directly against fast cached-shard outfmt 6 probes.
  The comparator also accepts an optional 13th raw `score` column, which avoids
  false mismatches when outfmt 6 rounds bit score display precision.
- No IaC changes.

## Validation Evidence

- `uv run pytest -q api/tests/test_blast_config_sharding.py api/tests/test_blast_submit_route_options.py`
- `uv run pytest -q api/tests/test_compare_blast_web_xml_outfmt6.py`
- Generated config smoke check includes:
  `options = -outfmt 6 -word_size 28 -dust yes -soft_masking false -searchsp 32156241807668`
- Fast AKS cached-shard probe with current Web RID `0K7GE593016` reproduced
  the Web raw score only with `-dust yes -soft_masking false`: raw score `448`,
  bit score `828.419`. Plain `-dust yes` and `-dust no` both produced raw score
  `462`, bit score `854.272`.