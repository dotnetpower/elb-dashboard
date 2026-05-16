# 2026-05-15 — Dev compose: surface host `az login` to api/worker/beat

## Motivation

The compose stack's api / worker / beat sidecars had **no Azure credential at
all**: no `~/.azure`, no `az` CLI on the image, no MSI endpoint. Every
`DefaultAzureCredential` chain failed and `/api/arm/subscriptions` (plus
every other `/api/arm/*` route) returned an empty list. From the SPA's
perspective the discovery wizard looked broken — "구독을 못 찾는데?".

In production this never happens because the Container App injects the
shared user-assigned MI `id-elb-control`. Locally there is no MI, so dev
needs a different bridge.

## User-facing change

* Subscription discovery now works in the local compose stack as soon as
  the developer has `az login`'d on the host.
* Dashboard wizard surfaces the host's subscriptions / RGs / storage
  accounts / ACRs without any extra setup beyond `az login`.

## API / IaC diff summary

* **`api/Dockerfile`** — added an opt-in `dev` stage on top of `runtime`:
  installs `azure-cli` (~100 MB) via the Microsoft apt repo, stays as
  root so the bind-mounted host `~/.azure` (owned by uid 1000) is fully
  readable/writable. Production images still build the `runtime` stage
  and stay slim — nothing in the prod image changed.
* **`scripts/dev/docker-compose.full.yml`** — api / worker / beat now
  build with `target: dev`, and each mounts the developer's
  `~/.azure` at `/root/.azure` (RW, not RO — az CLI rewrites
  `versionCheck.json` / `extensionIndex.json` on every invoke and an RO
  mount makes every `az` call fail). `AZURE_CONFIG_DIR=/root/.azure`
  is set on api so the cred chain finds the cache.

No production Bicep changed; this is dev-only ergonomics.

## Trade-offs / safety notes

* The dev container can write to the host's `~/.azure` (token refresh,
  version markers). That is by design — RO breaks `az` itself and the
  developer is using their own machine.
* Token refresh inside the container happens against the same cache the
  host uses, so the host's `az login` stays valid afterwards.
* `target: dev` is opt-in via compose; `azd` / production builds still
  resolve the `runtime` stage (no az CLI shipped to prod).

## Validation evidence

```
$ docker exec elb-control-local-api-1 az account show \
    --query '{name:name, id:id, tenantId:tenantId}'
{
  "id": "b052302c-4c8d-49a4-aa2f-9d60a7301a80",
  "name": "ME-MngEnvMCAP132261-moonchoi-1",
  "tenantId": "78716814-cb3c-4b74-8fa8-0688dbd41ec3"
}

$ curl -fsS http://127.0.0.1:18080/api/arm/subscriptions
[{"subscriptionId":"b052302c-4c8d-49a4-aa2f-9d60a7301a80",
  "displayName":"ME-MngEnvMCAP132261-moonchoi-1",
  "state":"Enabled",
  "tenantId":"78716814-cb3c-4b74-8fa8-0688dbd41ec3"}]
```

Backend pytest: `120 passed in 10.86s` (no regression).

## Companion frontend fix (same session)

`web/src/pages/Dashboard.tsx` — added a small effect so that if
`/api/arm/subscriptions` resolves successfully but with `[]`, the wizard
opens immediately instead of spinning forever on
"Discovering existing BLAST workspaces…". This complements the compose
fix: if the host has no `az login` (or the login has no subscriptions),
the SPA still drops the user into the manual-entry path instead of
hanging.

```tsx
useEffect(() => {
  if (!needsDiscovery) return;
  if (subsQuery.isSuccess && (subsQuery.data?.length ?? 0) === 0) {
    setDiscoveryDone(true);
    setShowWizard(true);
  }
}, [needsDiscovery, subsQuery.isSuccess, subsQuery.data]);
```

`npm run build` clean.
