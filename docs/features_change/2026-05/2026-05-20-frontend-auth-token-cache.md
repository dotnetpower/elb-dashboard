# Frontend Auth Token Cache

## Motivation

Dashboard polling and setup flows can issue many authenticated requests at once. The backend auth caches were intact, but the frontend API and direct ARM clients still called `acquireTokenSilent()` for each request.

## User-facing change

Authenticated dashboard calls reuse a short-lived in-memory access token until 60 seconds before expiry. Concurrent token acquisition is deduplicated so a burst of requests shares one MSAL silent-acquire call. A 401 response clears the relevant token cache.

## API and IaC diff summary

No API or IaC changes. The change is limited to the browser clients for `/api/*` and direct ARM requests.

## Validation evidence

- Reviewed backend caches: `api.auth._CLAIMS_CACHE`, `api.services.get_credential()`, and `api.services.azure_clients._get_mi_credential()` remain active.
- `cd web && npm run build`