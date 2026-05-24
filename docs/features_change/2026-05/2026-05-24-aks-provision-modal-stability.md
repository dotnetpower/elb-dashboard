# AKS Provision Modal Stability

## Motivation

The Create AKS Cluster dialog could appear to rapidly grow, shrink, and flicker while dashboard data and validation hints updated around the modal.

## User-facing Change

The AKS cluster creation modal now keeps a stable desktop height with only the body area scrolling. On phone-width layouts, it relies on the shared fullscreen dialog geometry instead of a separate dynamic viewport-height override. The transient Validating state is delayed slightly, so fast preflight checks no longer flash a one-frame status row.

## API/IaC Diff Summary

No API or IaC changes. Frontend-only layout adjustment in the AKS provisioning dialog and mobile dialog CSS.

## Validation Evidence

- `cd web && npm run build`
- Browser inspection confirmed the modal shell uses a fixed desktop height and scrolls the body instead of resizing around transient footer content.