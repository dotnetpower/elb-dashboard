# 2026-05-26 — ClusterCard "Provisioning failed" banner is transient again

## Motivation

A user reported that the dashboard's `AZURE KUBERNETES SERVICE CLUSTER`
card kept rendering a "Provisioning failed — Could not access resource
group rg-elb-cluster." banner even though the cluster had been
**cleanly deleted** and no new attempt was running. Refreshing the
browser brought the banner back, with no way to make it go away short
of clicking Dismiss in the very browser profile that hit the failure.

Root cause: the card had two persistent hydration sources for the
banner:

1. `localStorage` slot `elb_last_failed_provision_v1`, written by
   `useClusterProvisioning` whenever `provision_aks` FAILED.
2. Server-side `GET /api/aks/recent-failed-provisions?hours=24&limit=1`
   (JobState rows for the caller, 24 h window), queried on every
   mount.

Either source would re-surface the same failed-attempt row on the next
page load — and (1) was even mirrored across tabs via a `storage`
event listener. That contract is incompatible with "show error once,
at the time of failure".

## User-facing change

* The "Last attempt failed" / "Provisioning failed" banner now lives
  **only in React state for the current session**. It appears the
  moment `provision_aks` reports FAILURE / REVOKED, and disappears on:
  * browser refresh (React state is dropped), or
  * clicking Dismiss on the banner.
* The card no longer auto-hydrates a banner from `localStorage` or
  from `/api/aks/recent-failed-provisions` on mount.
* A cleanly deleted cluster no longer leaves a sticky error behind on
  the dashboard.

## Code diff summary

* `web/src/components/cards/ClusterCard/ClusterCard.tsx`
  * Removed `lastFailed` state, the mount-time hydration `useEffect`
    that read `localStorage` + called `aksApi.recentFailedProvisions`,
    the cross-tab `storage` listener, and the `lastFailedIsStale`
    computation + dismiss effect.
  * Removed the `<ProvisionErrorCard raw={lastFailed.raw} … />` render
    block at the bottom of the card.
  * Dropped the now-unused imports (`aksApi`, `loadDismissThreshold`,
    `loadLastFailedProvision`, `dismissLastFailedProvision`,
    `LastFailedProvision`, `useEffect`).
* `web/src/components/cards/ClusterCard/useClusterProvisioning.ts`
  * Removed the `saveLastFailedProvision({...})` call inside the task
    poll's FAILURE branch.
  * Removed the `dismissLastFailedProvision(Date.now())` call inside
    the "cluster Succeeded" branch (no longer needed).
  * Dropped the now-unused imports.

No API surface changes. No Bicep / IaC changes. The backend route
`/api/aks/recent-failed-provisions` and the `lastFailedProvision.ts`
localStorage helper are left in place (still covered by their own
unit tests); they are simply no longer wired into the card. Future
work that genuinely wants a sticky / cross-session error feed can
re-use them.

## Validation

* `cd web && npm run build` → ✓ built in 7.06 s, no TS errors.
* Manual UI logic trace: the banner is now driven solely by
  `prov.provError`, which is set in `useClusterProvisioning`'s task
  poller and cleared by `prov.resetError` (Dismiss / Retry) or by a
  page refresh. No mount-time fetch can re-introduce it.
