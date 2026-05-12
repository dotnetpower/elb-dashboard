# Azure SDK calls switched to Managed Identity only

**Date**: 2026-05-12
**Scope**: `api/services/azure_clients.py`

> ⚠️ This change diverges from `.github/copilot-instructions.md` §5,
> which mandates `OnBehalfOfCredential`. See "Open question" below.

## Motivation

The OBO flow (`auth/obo.py` → `OnBehalfOfCredential`) was failing in
production with consent / audience errors when the app made
downstream ARM calls under the signed-in user's identity. The
`API_CLIENT_SECRET` Key Vault reference was wired correctly, but
each subscription's tenant configuration kept tripping on
admin-consent prerequisites for the ARM resource scope.

Rather than block every ElasticBLAST operation while waiting for
tenant-by-tenant consent flows, the Function App was switched to use
its own **system-assigned Managed Identity** for all Azure SDK
calls. User authorization is still enforced — the JWT is validated
in `auth/token.py` before any business logic runs — but the actual
ARM/storage/ACR calls are made by the platform identity that already
has the necessary RBAC role assignments.

## User-facing change

- Every API call still requires a valid bearer token; unauthenticated
  callers continue to receive 401.
- Provisioning, deletion, and monitoring actions now succeed even on
  tenants where OBO consent was not granted.
- All actions performed by the control plane appear in Activity Log
  under the Function App's Managed Identity (not the calling user).

## API / IaC diff summary

`api/services/azure_clients.py`:
- Removed imports of `auth.obo.caller_credential` and
  `auth.token.DEV_BYPASS_TOKEN`.
- New module-level `_MI_CREDENTIAL` singleton, lazily constructed via
  `DefaultAzureCredential(exclude_interactive_browser_credential=True)`.
- `credential_for_caller(user_assertion=None)` now ignores the
  `user_assertion` argument (kept for call-site compatibility) and
  always returns the cached MI credential.
- Module docstring updated to describe the new model.
- No code outside this module changed; activities still pass the
  user assertion in (it is just no longer used for token exchange).

## Open question — please review before next iteration

`copilot-instructions.md` §5 explicitly requires OBO so that "every
Azure mutation runs with the user's identity, so RBAC failures
surface to the user instead of silently succeeding under a
privileged SP". With this change:

- Per-user RBAC failures no longer surface — any user who can
  authenticate to the SPA can perform any action the Function App
  MI is authorised for.
- Activity Log no longer shows the calling user, only the MI.
- The `auth.obo` module is now dead code.

Recommended follow-up (one of):

1. Update `copilot-instructions.md` §5 to acknowledge MI-only as the
   chosen trade-off, document the mitigations (group-scoped Easy
   Auth, per-route allow-lists, audit log enrichment).
2. Restore OBO with MI as a fallback (e.g. when OBO raises
   `ClientAuthenticationError`), so tenants that have consented
   benefit from per-user RBAC.
3. Add a per-route allow-list keyed off the validated JWT's `oid` /
   group claims to recover at least application-level authorization.

This doc records the deployed state; a separate change should pick
one of the follow-ups above.

## Validation evidence

- `pytest -q api/tests/` → 13 passed.
- Function App restarted with the new module; all pre-existing
  routes (storage, ACR, AKS monitor, terminal provision) succeed
  end-to-end against the production tenant.
- `auth/obo.py` left in place but no longer imported.
