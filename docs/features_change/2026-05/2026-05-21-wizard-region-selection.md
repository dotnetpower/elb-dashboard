# Setup wizard region selection

## Motivation

The setup wizard displayed the primary Azure region as a read-only value derived from the workload resource group's metadata location, so users could not choose a different deployment region for new resources.

## User-facing change

Step 2 of the setup wizard now includes an editable Primary Region selector. Selecting an existing workload resource group still suggests that group's Azure location, but users can override it before continuing. The confirmation step now treats the region as an explicit configuration value.

## API/IaC diff summary

- No backend API changes.
- No IaC changes.
- The existing `region` field continues to flow into Storage and ACR provisioning requests.

## Validation evidence

- `cd web && npm run build`