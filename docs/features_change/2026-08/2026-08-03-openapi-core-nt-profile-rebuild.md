---
title: OpenAPI core_nt profile rebuild
description: Rebuild the OpenAPI runtime from a verified source context after live Service Bus jobs ignored core_nt_safe and initialized the full database on every node.
tags: [blast, operate, release]
---

# OpenAPI core_nt profile rebuild

## Motivation

A four-way live [Azure Service Bus](https://learn.microsoft.com/azure/service-bus-messaging/service-bus-messaging-overview) validation submitted the curated `core_nt` request with `resource_profile=core_nt_safe`. All four requests were accepted and began within the same second, but every generated ElasticBLAST configuration omitted `db-partitions` and `db-partition-prefix`.

The jobs therefore initialized the full `core_nt` database on every node instead of using the prepared ten-shard layout. The full-database init path dominated runtime and created redundant setup work even though the dashboard warmup gate reported ten of ten nodes ready.

## User-facing change

The pinned OpenAPI runtime moves from `4.27` to `4.28`. The new image was built from a clean, verified sibling source context containing both:

- the `core_nt_safe` / `core_nt_precise` profile translation to a maximum of ten partitions; and
- the ElasticBLAST runtime patch hook used by the sharded local-SSD execution path.

Service Bus `core_nt` requests using the curated profile now generate a sharded configuration instead of paying the full-database initialization path.

## Runtime diff

- Built `elb-openapi:4.28` from sibling commit `352a1f4`.
- ACR build run `de4n` succeeded and pushed digest `sha256:210d0103c3ed2ec7a5e8d0cf12d3c9da9d8954662b55310bdb16f5088887c7af`.
- Updated the dashboard image pin only after the image push completed.
- No Storage network rule, Service Bus entity/configuration, SAS path, RBAC assignment, or Azure resource was created.

## Validation

- Pre-fix live run: four accepted jobs started within one second, but all four generated INI files lacked `db-partitions` and entered the full `core_nt` init path.
- The eight invalid validation jobs were cancelled through the sibling lifecycle API; no test job was left running.
- Post-rollout sharded execution, completion consumption, result manifest, and no-file download stream evidence are recorded after the live rerun.