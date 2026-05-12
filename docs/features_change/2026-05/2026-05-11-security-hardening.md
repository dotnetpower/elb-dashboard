# Security Hardening Audit — 60 Findings

**Date**: 2026-05-11

## Motivation

Full security/reliability audit of the codebase (API, frontend, OpenAPI service, infrastructure). 60 findings across CRITICAL/HIGH/MEDIUM/LOW. Code-verified each finding before fixing — some audit items were false positives (already defended in code).

## Fixes Applied

### CRITICAL / HIGH

| # | File | Fix |
|---|------|-----|
| 1 | `api/services/ssh_exec.py` | `AutoAddPolicy()` → `WarningPolicy()` (log unknown host keys instead of silently trusting) |
| 2 | `api/services/ssh_exec.py` | Added SSH keepalive 30s (detect dead connections) |
| 3 | `api/services/ssh_exec.py` | `client.close()` wrapped in try/except (prevent hang on close) |
| 4 | `api/services/network.py` | NSG: added outbound rules — `AllowOutboundAzure` (priority 1000) + `DenyOtherOutbound` (priority 4000) |
| 5 | OpenAPI `main.py` | CORS: `allow_origins=["*"]` → derived from `CONTROL_PLANE_URL` |
| 6 | OpenAPI `main.py` | Security headers middleware: `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` |

### MEDIUM

| # | File | Fix |
|---|------|-----|
| 7 | `api/orchestrators/submit_blast.py` | `MIN_POLLS_BEFORE_COMPLETE` fixed to 3 (was 1 when warmup enabled — trusted premature EXIT_CODE=0) |
| 8 | `api/services/sanitise.py` | Added patterns: Base64 blobs (≥40 chars), connection strings, password/secret values |
| 9 | OpenAPI `main.py` | Webhook retry: fire-and-forget → 3 attempts with exponential backoff (1s, 2s) |

### Verified as Already Defended (Not Fixed)

- `blast.py` job_id injection → `^[a-zA-Z0-9_-]+$` regex validation exists
- `monitoring.py` K8s API timeout → all calls have `timeout=10`
- `monitoring.py` temp file cleanup → custom `close()` hook exists
- `storage_data.py` path traversal → `..` and `/` checks exist
- `storage_window.py` disable failure → 3 retries + RuntimeError on exhaustion

## Validation

- 13 unit tests pass
- TypeScript build: 0 errors
- Python AST syntax check: all files OK
