---
title: Unreleased
description: ElasticBLAST Control Plane Unreleased release notes — feature-change notes that landed in this version.
tags:
  - release
---

# Unreleased

Feature-change notes added in `v0.3.0..HEAD`.

> **Warnings**
>
> - 101 `feat:`/`fix:` commits in range vs 67 new feature-change notes — 34 commit(s) may be missing a note (charter §13).

**Count:** 67

## Features

- `2026-09-11` — [Separate BLAST queue and processing time](../features_change/2026-09/2026-09-11-blast-timing-breakdown.md) ([`4becd25`](https://github.com/dotnetpower/elb-dashboard/commit/4becd254032c6b81e580e088f712041a61f3f402))
- `2026-09-10` — [OpenAPI sibling source publication](../features_change/2026-09/2026-09-10-openapi-sibling-source-publication.md) ([`73a6b7d`](https://github.com/dotnetpower/elb-dashboard/commit/73a6b7d9395688c0326f8b4185d8744cf8bb942e))
- `2026-09-10` — [Sequence-diversity result selection](../features_change/2026-09/2026-09-10-sequence-diversity.md) ([`d857f57`](https://github.com/dotnetpower/elb-dashboard/commit/d857f570351e76e4718310031a0f08f685ebd6bc))
- `2026-09-10` — [Expose sequence diversity in API tools](../features_change/2026-09/2026-09-10-sequence-diversity-api-surfaces.md) ([`e35aa14`](https://github.com/dotnetpower/elb-dashboard/commit/e35aa1490bb05636a5efa773c21c530488e8cf5e))
- `2026-09-09` — [Portable BLAST reproducibility package](../features_change/2026-09/2026-09-09-reproducibility-package.md) ([`3e3f6a6`](https://github.com/dotnetpower/elb-dashboard/commit/3e3f6a627981b38842aea0ef45eb16fdf91607f7))
- `2026-09-09` — [Split-job shard details](../features_change/2026-09/2026-09-09-split-job-shard-details.md) ([`036cbee`](https://github.com/dotnetpower/elb-dashboard/commit/036cbeecb8fc039801be9f2d88d35d2cd8478fa7))
- `2026-09-09` — [Completed BLAST result comparison](../features_change/2026-09/2026-09-09-completed-job-result-comparison.md) ([`61756e5`](https://github.com/dotnetpower/elb-dashboard/commit/61756e575fb54f71c073e14988e563e7cb497809))
- `2026-09-09` — [Evidence-based BLAST runtime and cost estimate](../features_change/2026-09/2026-09-09-evidence-based-runtime-cost-estimate.md) ([`4cfc7f7`](https://github.com/dotnetpower/elb-dashboard/commit/4cfc7f7205b34b6c30f42de2a9d10e31a48ee32f))
- `2026-09-09` — [Restore saved BLAST submit templates](../features_change/2026-09/2026-09-09-restore-blast-submit-templates.md) ([`8e829ac`](https://github.com/dotnetpower/elb-dashboard/commit/8e829ac4c3a18ea0cf99401ebad175c2208addf9))
- `2026-09-09` — [OpenAPI reference context resolver](../features_change/2026-09/2026-09-09-openapi-reference-context-resolver.md) ([`e799974`](https://github.com/dotnetpower/elb-dashboard/commit/e799974cbe211ccb1e92bdca8ccf40a0875bb951))
- `2026-08-28` — [NCBI Direct database generations](../features_change/2026-08/2026-08-28-ncbi-direct-db-generations.md) ([`f4b58c1`](https://github.com/dotnetpower/elb-dashboard/commit/f4b58c1bce1c70c6820555918fbffe5dab42c399))
- `2026-08-27` — [Automatic DB order oracle lifecycle](../features_change/2026-08/2026-08-27-auto-db-order-oracle.md) ([`bce59ba`](https://github.com/dotnetpower/elb-dashboard/commit/bce59bae732c26f62921446bfdd54d375950b0ab))
- `2026-08-27` — [Dashboard functional audit hardening](../features_change/2026-08/2026-08-27-dashboard-functional-audit.md) ([`bce59ba`](https://github.com/dotnetpower/elb-dashboard/commit/bce59bae732c26f62921446bfdd54d375950b0ab))
- `2026-08-27` — [DB order oracle cluster coordinates](../features_change/2026-08/2026-08-27-db-order-oracle-cluster-coordinates.md) ([`bce59ba`](https://github.com/dotnetpower/elb-dashboard/commit/bce59bae732c26f62921446bfdd54d375950b0ab))
- `2026-08-27` — [NCBI cloud mirror update pending](../features_change/2026-08/2026-08-27-ncbi-cloud-mirror-update-pending.md) ([`bce59ba`](https://github.com/dotnetpower/elb-dashboard/commit/bce59bae732c26f62921446bfdd54d375950b0ab))

## Fixes

- `2026-09-16` — [Quick deploy live manifest protection](../features_change/2026-09/2026-09-16-quick-deploy-live-manifest-protection.md) ([`67df3a4`](https://github.com/dotnetpower/elb-dashboard/commit/67df3a4befeeb7171f216eb6b8a546a33cc9e929))
- `2026-09-11` — [Bound Service Bus execution admission](../features_change/2026-09/2026-09-11-servicebus-admission-marker-index.md) ([`89904ce`](https://github.com/dotnetpower/elb-dashboard/commit/89904ce0be46e5b6208aa3bb89e6fce3b564a23d))
- `2026-09-11` — [Harden terminal message lifecycle rendering](../features_change/2026-09/2026-09-11-message-lifecycle-prefork-hardening.md) ([`aecaa61`](https://github.com/dotnetpower/elb-dashboard/commit/aecaa61397fedda1773a032eb76b4c27a58e5153))
- `2026-09-10` — [Remove the sequence-diversity pool hard limit](../features_change/2026-09/2026-09-10-sequence-diversity-pool-limit-removal.md) ([`955cec3`](https://github.com/dotnetpower/elb-dashboard/commit/955cec3fe138d4df2966e46b4b517ccb0b708472))
- `2026-09-10` — [Harden sequence-diversity merge publication](../features_change/2026-09/2026-09-10-sequence-diversity-hardening-rounds.md) ([`04de3a2`](https://github.com/dotnetpower/elb-dashboard/commit/04de3a27bab9ea6bcb08e0a8fbcf06b94e3c4778))
- `2026-09-10` — [Reduce OpenAPI BLAST completion latency](../features_change/2026-09/2026-09-10-openapi-runtime-latency-hardening.md) ([`ace2f31`](https://github.com/dotnetpower/elb-dashboard/commit/ace2f31d8d89e558ab5b0d5040f3965f1db641ce))
- `2026-09-09` — [Time-index reconciliation hardening](../features_change/2026-09/2026-09-09-time-index-reconcile-hardening.md) ([`86651e2`](https://github.com/dotnetpower/elb-dashboard/commit/86651e2407f5fc08608074cfd5feef008198d9d1))
- `2026-09-09` — [Platform network-lockdown validation](../features_change/2026-09/2026-09-09-platform-network-lockdown-validation.md) ([`184a818`](https://github.com/dotnetpower/elb-dashboard/commit/184a8185eee34ae13703ca7a42faf7a62679469a))
- `2026-09-09` — [Stopped-cluster external job-list gate](../features_change/2026-09/2026-09-09-stopped-cluster-job-list-gate.md) ([`bb57707`](https://github.com/dotnetpower/elb-dashboard/commit/bb577070b5a12055128547a67753c105fb89bbe0))
- `2026-09-09` — [OpenAPI result readiness and selection contracts](../features_change/2026-09/2026-09-09-openapi-result-contract-hardening.md) ([`9d30a2f`](https://github.com/dotnetpower/elb-dashboard/commit/9d30a2f31d208ace0bd0e56c063583d6e1a96ceb))
- `2026-09-07` — [Separate shard candidate and result limits](../features_change/2026-09/2026-09-07-shard-candidate-result-cap.md) ([`37b0d54`](https://github.com/dotnetpower/elb-dashboard/commit/37b0d5429dd497b3576933bb12af5f19dd5a6394))
- `2026-09-07` — [OpenAPI derives a missing precise search space](../features_change/2026-09/2026-09-07-openapi-search-space-fallback.md) ([`b199973`](https://github.com/dotnetpower/elb-dashboard/commit/b199973f6e73c11af7a33d31d32b7b01fd51b946))
- `2026-09-07` — [Separate browser and server telemetry status](../features_change/2026-09/2026-09-07-telemetry-status-separation.md) ([`94eb0ef`](https://github.com/dotnetpower/elb-dashboard/commit/94eb0eff63fd9c81594d444903141d6891bf56b3))
- `2026-09-07` — [App Insights error hunt and runtime noise hardening](../features_change/2026-09/2026-09-07-app-insights-error-hunt.md) ([`3e60703`](https://github.com/dotnetpower/elb-dashboard/commit/3e607037d770a4d10755d07c534b01bcfced77be))
- `2026-09-05` — [Web BLAST taxonomy-filtered statistics parity](../features_change/2026-09/2026-09-05-web-blast-filtered-statistics.md) ([`ef3c3ab`](https://github.com/dotnetpower/elb-dashboard/commit/ef3c3ab358485d0893f944112e41fb206f430ec0))
- `2026-09-04` — [Fresh same-snapshot Web BLAST reference truth](../features_change/2026-09/2026-09-04-fresh-web-blast-reference-truth.md) ([`3b8b125`](https://github.com/dotnetpower/elb-dashboard/commit/3b8b1253f1fb86d8ceae8b0e4089bf35c2eae503))
- `2026-09-02` — [Scale near-miss preservation for 5,000-hit shard merges](../features_change/2026-09/2026-09-02-sharded-near-miss-proportional-reservation.md) ([`0ce4027`](https://github.com/dotnetpower/elb-dashboard/commit/0ce40277adeddf62e98a8c0ca9d0ab116e6ab2ad))
- `2026-09-02` — [Exact full-DB hitlist selection for sharded BLAST](../features_change/2026-09/2026-09-02-sharded-full-db-exact-hitlist.md) ([`2621836`](https://github.com/dotnetpower/elb-dashboard/commit/2621836206fad6a13e3cb9016f07961a30a61ede))
- `2026-09-02` — [Recover stale-open ACR build access](../features_change/2026-09/2026-09-02-acr-stale-open-recovery.md) ([`4381bd8`](https://github.com/dotnetpower/elb-dashboard/commit/4381bd83e2ef0774d9efe3fade53cf7dc954a164))
- `2026-09-02` — [Strict same-snapshot Web BLAST parity gate](../features_change/2026-09/2026-09-02-strict-web-blast-parity-gate.md) ([`4381bd8`](https://github.com/dotnetpower/elb-dashboard/commit/4381bd83e2ef0774d9efe3fade53cf7dc954a164))
- `2026-09-01` — [Preserve near-miss variants at sharded BLAST cutoffs](../features_change/2026-09/2026-09-01-sharded-near-miss-preservation.md) ([`45735bf`](https://github.com/dotnetpower/elb-dashboard/commit/45735bf998cee0f683a3e5b3126170739a592293))
- `2026-08-29` — [NCBI Direct live rollout hardening](../features_change/2026-08/2026-08-29-ncbi-direct-live-rollout.md) ([`82e7758`](https://github.com/dotnetpower/elb-dashboard/commit/82e77584fc4ce847f45aeab027fadf0e292da9e6))
- `2026-08-29` — [OpenAPI Storage convergence and Direct progress](../features_change/2026-08/2026-08-29-openapi-storage-and-direct-progress.md) ([`7d4bdb2`](https://github.com/dotnetpower/elb-dashboard/commit/7d4bdb25ec6a5b2bf701327bf12ad922fb5c5582))
- `2026-08-28` — [About build identity alignment](../features_change/2026-08/2026-08-28-about-build-identity.md) ([`820b685`](https://github.com/dotnetpower/elb-dashboard/commit/820b685669c98607c35dd1b6ebd1d39fca937cf6))
- `2026-08-28` — [OpenAPI proxy schema cleanup](../features_change/2026-08/2026-08-28-openapi-proxy-schema.md) ([`cb18b2c`](https://github.com/dotnetpower/elb-dashboard/commit/cb18b2c0a1a6ed21ac21ffeb50193244745d8156))
- `2026-08-28` — [Commit update check notification](../features_change/2026-08/2026-08-28-commit-update-toast.md) ([`ae493a5`](https://github.com/dotnetpower/elb-dashboard/commit/ae493a56b6f13c2ce86eb1ba46de3ede25076815))
- `2026-08-28` — [Fast-deploy platform ACR convergence](../features_change/2026-08/2026-08-28-fast-deploy-platform-acr.md) ([`4df839c`](https://github.com/dotnetpower/elb-dashboard/commit/4df839c0ff44acb5d0b2f7ceced04c8df2a7e960))
- `2026-08-28` — [Self-upgrade build number parity](../features_change/2026-08/2026-08-28-self-upgrade-build-number.md) ([`573a342`](https://github.com/dotnetpower/elb-dashboard/commit/573a3421a69ff57867a0f4a24900c1841f0bae7c))
- `2026-08-27` — [Live Wall workspace ID deployment repair](../features_change/2026-08/2026-08-27-live-wall-workspace-id.md) ([`3e82c36`](https://github.com/dotnetpower/elb-dashboard/commit/3e82c36c79deaa4a899e89f01a9c922d96dc5017))
- `2026-08-26` — [OpenAPI proxy token resynchronization](../features_change/2026-08/2026-08-26-openapi-proxy-token-resync.md) ([`3491915`](https://github.com/dotnetpower/elb-dashboard/commit/349191593c7ba80ee7bb4348e9e8261f7986b529))
- `2026-08-26` — [OpenAPI ElasticBLAST script reconciliation](../features_change/2026-08/2026-08-26-openapi-script-configmap-reconciliation.md) ([`d7e36b5`](https://github.com/dotnetpower/elb-dashboard/commit/d7e36b512d6c9d0a71d50ea12b0e7c41c579f4ae))
- `2026-08-26` — [Service Bus terminal identity projection](../features_change/2026-08/2026-08-26-servicebus-terminal-identity-projection.md) ([`ca98d21`](https://github.com/dotnetpower/elb-dashboard/commit/ca98d211901a1ed49ac1021fb648a7768af7e955))
- `2026-08-26` — [Service Bus oversized request preflight](../features_change/2026-08/2026-08-26-servicebus-oversized-preflight-order.md) ([`9319cb6`](https://github.com/dotnetpower/elb-dashboard/commit/9319cb61b357f140b6d43921249a77d8c4a42475))
- `2026-08-26` — [Stopped AKS periodic task gates](../features_change/2026-08/2026-08-26-stopped-aks-periodic-gates.md) ([`16f68f6`](https://github.com/dotnetpower/elb-dashboard/commit/16f68f6217d78eb63c6d083a6eb083ef11f31da1))
- `2026-08-25` — [Service Bus request queue reliability hardening](../features_change/2026-08/2026-08-25-servicebus-request-queue-hardening.md) ([`2d33851`](https://github.com/dotnetpower/elb-dashboard/commit/2d3385134ecd4b42d69e0cf244c7326e78992a35))
- `2026-08-25` — [CLI upgrade preflight compatibility](../features_change/2026-08/2026-08-25-cli-upgrade-preflight-compatibility.md) ([`1705182`](https://github.com/dotnetpower/elb-dashboard/commit/170518236303fb259e367e1d4744789d6b7c1006))
- `2026-08-25` — [Local Storage auto-open default-off hardening](../features_change/2026-08/2026-08-25-local-storage-auto-open-default-off.md) ([`a26cd5d`](https://github.com/dotnetpower/elb-dashboard/commit/a26cd5d1796100d194f6dac9af34187672fdd1ae))
- `2026-08-25` — [Service Bus three-state deployment guidance](../features_change/2026-08/2026-08-25-servicebus-three-state-guidance.md) ([`389f170`](https://github.com/dotnetpower/elb-dashboard/commit/389f170b3b1f58d2c45b005d584772ce3fa83691))
- `2026-08-25` — [Prepare database ownership fences](../features_change/2026-08/2026-08-25-prepare-db-owner-fences.md) ([`a838157`](https://github.com/dotnetpower/elb-dashboard/commit/a8381574702359907931d58c6bb18eda0025d056))
- `2026-08-24` — [Service Bus shard initialization hardening](../features_change/2026-08/2026-08-24-servicebus-shard-init-hardening.md) ([`5141efa`](https://github.com/dotnetpower/elb-dashboard/commit/5141efaff67aa7d4726b80c52ba9eb595c145b40))
- `2026-08-23` — [Kubernetes runtime and BLAST submit hardening](../features_change/2026-08/2026-08-23-k8s-runtime-submit-hardening.md) ([`961105c`](https://github.com/dotnetpower/elb-dashboard/commit/961105cbc3aae90cb5bd3f0395dc984537d1377e))
- `2026-08-21` — [Service Bus failed-start admission recovery](../features_change/2026-08/2026-08-21-servicebus-start-failure-recovery.md) ([`790b41b`](https://github.com/dotnetpower/elb-dashboard/commit/790b41b93cf1ff65ebcc120bc0e44f1e58ce17a9))
- `2026-08-05` — [Bound the GitHub Release body so a large release can publish](../features_change/2026-08/2026-08-05-release-body-size-limit.md) ([`2ec7e16`](https://github.com/dotnetpower/elb-dashboard/commit/2ec7e16ac48736583f21b8879a1970a716f88c9f))
- `2026-08-05` — [Keep the build stamp visible on laptop widths](../features_change/2026-08/2026-08-05-topbar-build-stamp-visibility.md) ([`47aea8a`](https://github.com/dotnetpower/elb-dashboard/commit/47aea8a00f1831211652970f6572266a739e3d9f))
- `2026-08-05` — [Stop the api sidecar OOM loop by returning freed glibc arenas](../features_change/2026-08/2026-08-05-api-sidecar-arena-reclaim.md) ([`c13f2ca`](https://github.com/dotnetpower/elb-dashboard/commit/c13f2ca2079dc833a3c10656c2ab829c9ee20f0b))
- `2026-08-05` — [Page the cluster-wide Kubernetes LISTs that OOM-killed the api sidecar](../features_change/2026-08/2026-08-05-k8s-list-paging.md) ([`6641dbd`](https://github.com/dotnetpower/elb-dashboard/commit/6641dbd52820ae5639cd6a6fa559977a76bc0523))
- `2026-08-05` — [Cap the per-job result-read memory budget](../features_change/2026-08/2026-08-05-result-read-budget.md) ([`32a8f28`](https://github.com/dotnetpower/elb-dashboard/commit/32a8f28bca43487d5bca89a8d54f5d0382891d16))
- `2026-08-05` — [Stop the Query ID header contradicting the FASTA preview](../features_change/2026-08/2026-08-05-query-label-placeholder-recovery.md) ([`e56ec9d`](https://github.com/dotnetpower/elb-dashboard/commit/e56ec9d39690d9c4cbdc1d602cc0cce47e83de46))

## Other

- `2026-09-09` — [Production mypy debt ratchet](../features_change/2026-09/2026-09-09-mypy-debt-ratchet.md) ([`a14800d`](https://github.com/dotnetpower/elb-dashboard/commit/a14800dbe4fccbeea528beae57a2d893e14e667e))
- `2026-09-09` — [OpenAPI compatibility gate and generated TypeScript declarations](../features_change/2026-09/2026-09-09-openapi-contract-generated-types.md) ([`f64dbf5`](https://github.com/dotnetpower/elb-dashboard/commit/f64dbf529bda04fdb257a74bcc88009b69881a2e))
- `2026-09-09` — [Additive BLAST expansion hardening](../features_change/2026-09/2026-09-09-additive-expansion-hardening.md) ([`dbe2c6d`](https://github.com/dotnetpower/elb-dashboard/commit/dbe2c6dfb0dff201cb234c47f55ca1f2eb47137b))
- `2026-08-25` — [Frontend dependency hardening](../features_change/2026-08/2026-08-25-frontend-dependency-hardening.md) ([`00ee9ec`](https://github.com/dotnetpower/elb-dashboard/commit/00ee9ec655f46beb45d20175313cc567d2e7a8c5))
