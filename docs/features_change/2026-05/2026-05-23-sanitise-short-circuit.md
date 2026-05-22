# sanitise — short-circuit + factored GUID redactor

## Motivation
`sanitise(text)` ran every input through 8 regex substitutions and an
inline lambda even when no sensitive substring was present. The function
is called for every NDJSON line streamed from `terminal_exec`, every
audit_log row, every error-branch HTTP body — so a payload with no
secret markers paid the full 8-pass cost.

## User-facing change
None. Same redaction output. Lower steady-state CPU on the audit /
streaming / log paths.

## API / IaC diff
* `api/services/sanitise.py`
  * Added `_FAST_TRIGGER_RE` (single-pass alternation that matches any
    of the secret-marker prefixes: SAS / Bearer / account-key /
    DefaultEndpoints / password / 40+ base64) and `_GUID_FAST_RE`
    (cheap pre-check for the hex-dash prefix).
  * `sanitise()` runs the fast pre-checks first; if neither secret
    markers nor GUID-like hex are present, returns early with at most
    one optional ANSI strip.
  * ANSI strip only fires when `\x1b` is actually in the input.
  * GUID redactor extracted from inline `lambda m: m.group(0)[:8] + "…"`
    into a module-level `_redact_guid` function so `re.sub` keeps a
    stable callable reference instead of constructing the lambda each
    call.

## Validation
* `uv run pytest -q api/tests -k sanitise` — 17 passed.
* `uv run ruff check api/services/sanitise.py` — clean.
