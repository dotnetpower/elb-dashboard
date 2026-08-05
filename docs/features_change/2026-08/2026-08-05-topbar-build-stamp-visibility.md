---
title: Keep the build stamp visible on laptop widths
description: The topbar overflow ladder hid the whole version stamp below 1400 px, so the running build was invisible on every common laptop. Shed only the commit SHA there and drop the stamp entirely at the drawer breakpoint instead.
tags: [ui, operate]
---

# Keep the build stamp visible on laptop widths

## Motivation

The topbar progressive-disclosure ladder added with the light-theme/overflow fix
sheds decorative chrome widest-first so the bar always fits. Its second tier was:

```css
@media (max-width: 1400px) {
  .layout__logo-version { display: none; }
}
```

1400 CSS px is not a narrow viewport — 1366, 1440 and a 1536-px-wide window at
125 % scaling are all below it. The practical effect was that the version stamp
next to "Control Plane" was **invisible on essentially every laptop**, which was
noticed immediately after the v0.3.0 rollout: the stamp rendered correctly
(`v0.3.1 · 2ec7e16a` in the DOM at a 1084 px viewport) but computed to
`display: none`.

"Which build am I looking at?" is the single thing an operator reads off the
topbar — after a deploy it is how you confirm the rollout actually landed. It
should not be the second thing shed, ahead of the "Live" pill and the latest-job
chip.

## User-facing change

The stamp now degrades instead of disappearing:

| Viewport | Topbar stamp |
| --- | --- |
| > 1400 px | `v0.3.1 · 2ec7e16a` (unchanged) |
| 941–1400 px | `v0.3.1` — the version stays, the short SHA sheds |
| ≤ 940 px | hidden (drawer tier — the topbar keeps only the logo + account controls) |

The full detail (release, build, build number, commit, build time) is unchanged
in the logo tooltip, and the Settings footer still shows the complete stamp.

## Change summary

* [web/src/components/Layout.tsx](../../../web/src/components/Layout.tsx): the
  short SHA moves into its own `layout__logo-sha` span so CSS can shed just that
  half.
* [web/src/components/Layout.css](../../../web/src/components/Layout.css): the
  1400 px tier now hides `.layout__logo-sha` instead of `.layout__logo-version`;
  a new rule inside the existing 940 px drawer block hides the stamp entirely.
  The shedding-ladder comment is updated to match.

No version-computation change — `formatBuildVersion` and the `APP_VERSION` /
`APP_BUILD_NUMBER` / `GIT_COMMIT` build args are untouched.

## Validation

* `cd web && npm run build` — clean.
* `npx eslint src/components/Layout.tsx` — clean.
* `npm test -- --run` — 108 files / 978 tests passed.
* Live check before the fix at a 1084 px viewport:
  `document.querySelector('.layout__logo-version')` →
  `textContent "v0.3.1 · 2ec7e16a"`, `getComputedStyle(...).display === "none"`.
  Re-checked after the frontend redeploy that the version is rendered and the
  SHA span is the only hidden half.
