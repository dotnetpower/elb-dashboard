---
title: New Search "Include taxonomy columns" toggle (taxid + scientific name)
description: >-
  The New Search Algorithm Parameters section gains an "Include taxonomy
  columns" toggle that emits the verified canonical `-outfmt 7 std staxids
  sscinames` specifier, so a researcher can get subject taxid + scientific name
  columns (including under sharding) without hand-crafting additional_options.
tags:
  - blast
  - ui
---

# New Search "Include taxonomy columns" toggle

## Motivation

Issue [#29](https://github.com/dotnetpower/elb-dashboard/issues/29) item #4. The
New Search Output-format control is an integer dropdown (`form.outfmt: number`)
and cannot express a field-level specifier, so a researcher who wanted the
subject **taxid / scientific name** columns had to hand-write
`additional_options` with the exact `-outfmt 7 std staxids sscinames` string and
know the YAML-quoting hazard. The multi-token outfmt runtime is now verified
end-to-end live (elb-openapi 4.22; see the finalizer rebuild note), so the UI can
expose it safely.

## User-facing change

- The **Algorithm Parameters** section now has an **"Include taxonomy columns
  (taxid + scientific name)"** checkbox under Output format.
- When checked:
  - It forces the Output format to `7 — Tabular + comments` (and disables the
    dropdown, since the format is now fixed) so the visible format matches the
    emitted specifier.
  - The submit emits the verified canonical **UNQUOTED** specifier
    `-outfmt 7 std staxids sscinames` via `additional_options`. `std` leads so
    the `qseqid` column stays first (the shard merge groups by qseqid).
  - The integer `outfmt` field is **omitted** from the request so the options
    string carries exactly one `-outfmt` flag — a double flag would make the
    shard merge's parser read only the leading code and drop the staxids /
    sscinames columns.
- A user-supplied `-outfmt` in additional_options still wins (the helper
  de-dupes on the flag), so power users keep full control.
- Works with sharding: the merged result keeps the extended `# Fields:` header
  (taxid + scientific name reach the dashboard Scientific Name column / Taxonomy
  tab). Requires a database that ships taxonomy data (e.g. `core_nt`).

## API / IaC diff summary

- [web/src/pages/blastSubmitModel.ts](../../../web/src/pages/blastSubmitModel.ts)
  — new `outfmt_taxonomy_columns: boolean` form field (default `false`).
- [web/src/pages/blastSubmit/AlgorithmParametersSection.tsx](../../../web/src/pages/blastSubmit/AlgorithmParametersSection.tsx)
  — the toggle checkbox; disables the Output-format dropdown when on.
- [web/src/pages/blastSubmit/useSubmitMutation.ts](../../../web/src/pages/blastSubmit/useSubmitMutation.ts)
  — `buildEffectiveAdditionalOptions` appends `-outfmt 7 std staxids sscinames`
  when the toggle is on; the submit request sends `outfmt: undefined` in that
  case.
- No backend behaviour change — the internal submit already accepts the
  multi-token specifier via `additional_options` and omits `-outfmt` when the
  `outfmt` field is absent. No IaC change.

## Validation evidence

- `cd web && npm test -- --run src/pages/blastSubmit/taxonomyOutfmt.test.ts` —
  3 passed (off → no `-outfmt`; on → canonical unquoted specifier; user `-outfmt`
  not doubled).
- `cd web && npm test -- --run src/pages/blastSubmit/` — 203 passed.
- `cd web && npm run build` — clean.
- `uv run pytest -q api/tests/test_blast_config_sharding.py` — 49 passed
  (new `test_taxonomy_columns_path_omits_integer_outfmt_field`: omitted integer
  `outfmt` + multi-token `additional_options` → exactly one `-outfmt`, sharding
  gate engages).
- The emitted `-outfmt 7 std staxids sscinames` wire format is the same one
  verified live end-to-end on `elb-cluster-01` (merged `# Fields:` header carried
  `subject tax ids` / `subject sci names`, taxid `562` / `Escherichia coli`).
