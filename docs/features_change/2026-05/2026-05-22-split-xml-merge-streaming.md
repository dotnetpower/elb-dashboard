# Split-parent XML merge — streaming rewrite

## Motivation
`_build_parent_split_xml_result_bytes` materialized the entire merged BLAST
XML tree in worker memory before gzip-compressing it:

* `gzip.decompress(...)` of each child's full gzipped XML
* `ET.fromstring(...)` of the decompressed payload (full DOM)
* `copy.deepcopy(child_root)` for the base + `copy.deepcopy(iteration)` per
  child Iteration element accumulated under one growing `base_iterations`
* `ET.ElementTree(base_root).write(BytesIO)` then `gzip.compress(...)` on
  the final bytes

Worst case (100-shard × 200 MB per child) = >20 GB resident in one worker
just to merge a single split parent. Defeats the careful chunk-based
`stream_blob_bytes` upload path that already existed for the tabular case.

The companion helper `_read_split_child_merged_result_bytes` defeated the
streaming download with a `b"".join(stream_blob_bytes(...))`.

## User-facing change
None. Same merged BLAST XML is produced (same `BlastOutput_program`,
`BlastOutput_version`, `BlastOutput_iterations` content, same renumbered
`Iteration_iter-num`). Worker RSS stays bounded by one Iteration element +
gzip window regardless of child count or per-child file size.

## API / IaC diff
* New `_iter_parent_split_xml_chunks(...)` generator in
  `api/tasks/blast/split_pipeline.py`:
  - Streams each child gzip blob through `_GeneratorByteReader`
    (`stream_blob_bytes` → `gzip.GzipFile(fileobj=…)` → `ET.iterparse`).
  - Saves only the first child's `BlastOutput_*` metadata tags, emits a
    rebuilt header with `BlastOutput_db` overridden once.
  - For each `<Iteration>` end event: renumber, serialize, feed to
    `zlib.compressobj(wbits=16+MAX_WBITS)`, yield the compressed bytes,
    `.remove()` the element from its parent and `.clear()` it so the
    iterparse tree never accumulates.
  - Caller (`_write_split_parent_result_artifacts`) passes the generator
    directly to `upload_blob_bytes` — no `[buffered_bytes]` wrap.
* Legacy `_build_parent_split_xml_result_bytes(...)` retained as a thin
  shim that materializes the streaming generator. Kept solely for the
  existing test fixtures and `api.tasks.blast.__init__` re-export contract
  so external monkeypatch sites do not break.
* `_GeneratorByteReader` added — minimal read-only binary file-like over a
  `bytes` iterator. Used by the streaming merge to chain
  `stream_blob_bytes → GzipFile → iterparse` without materializing the
  decompressed payload.
* `__all__` export and `api/tasks/blast/__init__.py` re-export updated to
  surface `_iter_parent_split_xml_chunks`.

## Validation
* `uv run pytest -q api/tests/test_blast_tasks.py` — 120 passed (XML merge
  test `test_write_split_parent_result_artifacts_merges_child_xml`
  exercises the new path; output schema unchanged).
* `uv run ruff check api/tasks/blast/split_pipeline.py
  api/tasks/blast/__init__.py` — clean.
