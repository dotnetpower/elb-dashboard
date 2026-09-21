---
title: Settle concurrent ACR build access before image pulls
description: Concurrent deploys now wait for an already-open registry policy to reach managed build agents before starting ACR Tasks.
tags:
  - infra
  - operate
  - security
---

# Settle concurrent ACR build access before image pulls

## Motivation

A push-triggered [Azure Container Registry](https://learn.microsoft.com/azure/container-registry/)
build overlapped a local API deployment. The workflow observed the registry's
ARM policy as `Enabled/Allow` and skipped the propagation delay, but one managed
build agent still saw the previous firewall policy while pulling the private
terminal base image. The terminal build failed with an IP-denied response while
the other image builds succeeded.

## User-Facing Change

Concurrent image builds no longer start immediately merely because another
process has already opened ACR. They wait through the same bounded propagation
settle used by the original opener and verify the policy again before scheduling
build work.

## API / IaC Diff Summary

- `acr_ensure_build_access` now performs pre-settle and post-settle policy checks
  in the already-open branch.
- The existing restore lease behavior is unchanged: active builds keep access
  open, while the final idle owner or recovery workflow restores
  `Disabled/Deny/AzureServices`.
- No Container App, RBAC, network resource, or application API changed.

## Validation Evidence

- The failed workflow's terminal image build showed a private-base pull denied
  immediately after the concurrent process observed ACR as already open.
- The ACR build-access subprocess suite passed with 24 tests, including a new
  regression that requires a second policy check after the already-open settle.
- ACR was confirmed idle and restored to
  `publicNetworkAccess=Disabled`, `defaultAction=Deny`, and
  `networkRuleBypassOptions=AzureServices` after the incident.