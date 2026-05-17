# ACR Build Network Unlock

## Motivation

Locked-down environments keep the platform ACR on private networking. During
`azd provision`, the postprovision hook uses `az acr build`, whose build agents
must log in to the registry. With ACR public network access disabled, those
agents fail with `client with IP ... is not allowed access` before any image can
be built.

## User-Facing Change

The postprovision hook now temporarily enables public network access on the
platform ACR only while remote image builds run, then restores public network
access to `Disabled` with `defaultAction=Deny` before exiting.

## API / IaC Diff Summary

- Added a trap-protected ACR network restore step to `scripts/dev/postprovision.sh`.
- The storage account and Key Vault lockdown posture is unchanged.

## Validation Evidence

- `az acr show --query '{publicNetworkAccess:publicNetworkAccess,defaultAction:networkRuleSet.defaultAction}'`
- `azd provision --no-prompt`