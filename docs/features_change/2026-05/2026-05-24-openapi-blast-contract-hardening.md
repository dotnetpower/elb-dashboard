# OpenAPI BLAST Contract Hardening

## Motivation

The external [OpenAPI](https://www.openapis.org/) facade for [BLAST](https://blast.ncbi.nlm.nih.gov/doc/blast-help/) submit, status, and result downloads was functional, but several contracts were implicit or upstream-shaped. External callers need stable submit correlation, explicit public status values, validated inline FASTA, and result file metadata that can be used without knowing dashboard-internal Storage blob paths.

## Fifty-Point Critique

1. Submit returned upstream status strings without a stable public vocabulary.
2. Status returned `completed` from some upstreams even though the public facade documents `success`.
3. Failed variants such as `failure`, `error`, and `timeout` were not normalized.
4. Running variants such as `submitted`, `submitting`, and `finalizing` leaked implementation phases.
5. Queued variants such as `accepted` and `pending` were not collapsed to `queued`.
6. Submit did not guarantee `submission_source` in the response when upstream omitted it.
7. Submit ignored caller-provided `external_correlation_id` even though the public example documents it.
8. The trusted `submission_source` guard was correct but undocumented in response shaping.
9. Direct external callers could not reliably join their own correlation id to later status records.
10. `/api/blast/jobs` inline FASTA submit and `/api/v1/elastic-blast/submit` could diverge in response shape.
11. Inline FASTA was only hashed for metadata and invalid FASTA could reach the sibling service.
12. Sequence data before the first FASTA header was not rejected at the facade boundary.
13. Empty FASTA records were not rejected at the facade boundary.
14. Duplicate FASTA query ids were not rejected at the facade boundary.
15. `is_inclusive=true` without `taxid` produced an ambiguous taxonomy request.
16. `taxid` without `is_inclusive` left inclusion semantics implicit.
17. `outfmt` was correctly fixed to XML, but status/result routes did not reinforce XML result expectations.
18. Result file entries could expose `name` but not `filename`.
19. Result file entries could expose `size` but not `size_bytes`.
20. Result file entries could omit `format`, making parseability ambiguous.
21. Result file entries could omit `file_id`, breaking the documented file download path.
22. The manifest and status result shapes were not aligned.
23. Local and external result file metadata used different alias sets.
24. Manifest schema version stayed stable but did not expose compatibility aliases.
25. The facade did not guarantee `db_name` when upstream returned only `db`.
26. The facade did not guarantee `program` when upstream omitted it on submit response.
27. The facade did not reserve `blast_version` and `db_version` keys when upstream had not filled them yet.
28. Submit accepted async work but did not make the accepted-vs-complete distinction obvious in every response.
29. The cURL example used a string priority while the schema requires an integer.
30. The status route was a raw proxy instead of a contract boundary.
31. The submit route was a raw proxy instead of a contract boundary.
32. The canonical dashboard job submit wrapper repeated external response shaping logic.
33. Correlation metadata could differ between the two submit entry points.
34. Public response fields depended on the exact sibling service version.
35. Older sibling responses with `name`/`size` file fields were less client-friendly.
36. Newer sibling responses with `filename`/`size_bytes` were not projected into dashboard-style manifests.
37. Clients had to branch between result manifest and status result file shapes.
38. Tests covered non-XML `outfmt` rejection but not invalid FASTA rejection.
39. Tests covered taxonomy pass-through but not inclusive defaulting.
40. Tests covered status forwarding but not public status normalization.
41. Tests covered manifest parseability but not alias compatibility fields.
42. The external facade could not prove it preserved caller correlation after submit.
43. Upstream `result.files[]` normalization was not tested.
44. Documentation and API behavior drifted on `external_correlation_id`.
45. Documentation and API behavior drifted on `priority` type.
46. Status responses could be less reproducibility-friendly when version keys were absent.
47. Unknown status strings fell through without a documented default.
48. The result file contract depended too much on upstream naming conventions.
49. The facade did not fail early for taxonomy combinations that have no effect.
50. The public API was correct in pieces, but too much of the contract lived in convention rather than code.

## User-Facing Change

- External submit preserves a valid caller `external_correlation_id` while keeping `submission_source` server-derived as `external_api`.
- External submit rejects malformed inline FASTA before contacting the sibling OpenAPI service.
- `taxid` now defaults `is_inclusive` to `true`; `is_inclusive` without `taxid` is rejected.
- Submit/status responses normalize status to `queued`, `running`, `success`, or `failed`.
- Result file entries returned by status and manifest include both `filename` / `name` and `size_bytes` / `size` aliases.
- The API Reference example now uses numeric `priority`.

## API/IaC Diff Summary

- API only. No infrastructure changes.
- Updated `/api/v1/elastic-blast/submit`, `/api/v1/elastic-blast/jobs/{job_id}`, and `/api/blast/jobs` inline FASTA response shaping.
- Updated result manifest helper to include file metadata aliases.

## Validation Evidence

- `uv run pytest -q api/tests/test_external_blast_api.py api/tests/test_blast_result_manifest.py` → 49 passed.
- `uv run pytest -q api/tests/test_route_contracts.py api/tests/test_smoke.py` → 83 passed.
- `uv run ruff check api/routes/elastic_blast.py api/routes/blast/submit.py api/services/blast/result_manifest.py api/main.py` → passed.
- `uv run ruff format --check api/routes/elastic_blast.py api/routes/blast/submit.py api/services/blast/result_manifest.py api/main.py` → already formatted.
- `uv run pytest -q api/tests` was also run for signal and reached 1434 passed / 2 failed. The failures were outside this change: `api/tests/test_response_contracts.py::test_preflight_returns_admission_decision` currently depends on unmocked BLAST database availability, and `api/tests/test_terminal_exec.py::test_run_truncates_stdout_above_cap` timed out in the local exec-server large-output stub.