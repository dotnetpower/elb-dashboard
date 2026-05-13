# Terminal SSH Hardening and Durable Jobs Recovery

## Motivation

The Dashboard and Primer Design routes had two production risks after the SSH-first rollout:

- Dashboard job polling could spend tens of seconds refreshing Durable orchestration state for multiple registry entries.
- Locking down the Function App's storage and terminal Key Vault public network access on the Consumption plan broke Durable Functions and prevented the backend from reading the terminal SSH password.

## User-facing change

The Jobs card now returns quickly even when Durable status refreshes are slow. Primer Design returns validation errors for invalid numeric input before contacting the terminal VM. Terminal-backed tools continue to use SSH-first execution when the terminal VM is running and the terminal Key Vault is reachable.

## API/IaC diff summary

- `list_blast_jobs()` now accepts both legacy list state and object state with a `jobs` list, ignores malformed job entries, and returns only jobs owned by the caller.
- Durable orchestration status enrichment in the Jobs list is now capped to eight in-flight jobs and guarded by a three-second per-status timeout.
- `blast_primer_design()` validates numeric parameters and rejects an invalid product size range with HTTP 400.
- `build_custom_database()` validates blob names and terminal identifiers before building shell commands.
- `ensure_ssh_from_function_app()` filters NSG sources to public IPv4 values, preserves existing valid entries, adds the live Function App egress IP when discoverable, and caps the allow-list at 64 entries.
- Production Consumption/Y1 requires the Function App storage account used by `AzureWebJobsStorage` to remain publicly reachable unless the app is moved to a private-network-capable hosting model.
- The terminal Key Vault must also remain reachable from the Function App for SSH-first execution; otherwise terminal tools fall back to slow Run Command and can hit the Static Web Apps backend timeout.

## Validation evidence

- `ruff check api/routes/blast_jobs.py api/routes/blast_tools.py api/services/network.py api/services/compute.py api/services/ssh_exec.py` passed.
- `pytest -q api/tests/test_models.py api/tests/test_passwords.py api/tests/test_sanitise.py` passed with 13 tests.
- Production API deployed successfully as `funcapp-202605140050.zip`; `/api/health` returned 200.
- Browser-originated `GET /api/blast/jobs` through Static Web Apps returned HTTP 200 in 973 ms after the status refresh cap.
- Browser-originated `POST /api/blast/primer-design` returned HTTP 200 in 1,954 ms after terminal Key Vault access was restored and the terminal VM was running.
- Invalid Primer Design input with `product_size_min > product_size_max` returned HTTP 400 in 477 ms with the expected error message.