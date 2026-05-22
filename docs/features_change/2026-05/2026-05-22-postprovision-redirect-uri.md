# Postprovision Redirect URI Guard

## Motivation

Reusing an existing [Microsoft Entra App Registration](https://learn.microsoft.com/entra/identity-platform/quickstart-register-app) during a numbered deployment can leave the new [Azure Container Apps](https://learn.microsoft.com/azure/container-apps/overview) origin out of the SPA redirect URI list. The deployed app then reaches the sign-in page but Microsoft identity platform rejects the request with `AADSTS50011`.

## User-facing change

`azd up` postprovision now always ensures the deployed Container App origin is present as a SPA redirect URI, even when `API_CLIENT_ID` was already configured and the App Registration is reused.

## API/IaC diff summary

- No API route or Bicep resource shape changes.
- `scripts/dev/postprovision.sh` now patches the App Registration through Microsoft Graph after resolving `API_CLIENT_ID`.

## Validation evidence

- Added the missing redirect URI for `https://ca-elb-dashboard-01.bluerock-6b7269fa.koreacentral.azurecontainerapps.io` through Microsoft Graph and confirmed it is returned in `spa.redirectUris`.
- `bash -n scripts/dev/postprovision.sh`