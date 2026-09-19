---
title: Dashboard UI Rules
description: Current dark/light theme tokens, flat panel styling, motion budget, and accessibility rules for the ElasticBLAST Control Plane SPA.
tags:
  - agent
  - ui
---

# Dashboard UI — Design Rules (detail)

> Re-verified 2026-09-16 against `web/src/theme/glass.css`. The historical
> `--glass-*` names remain compatibility aliases; the current UI is flat rather
> than heavily frosted.

The dark theme follows a restrained Grafana-like operational palette. The light
theme follows VS Code Light Modern: white panels on a near-white canvas,
hairline borders, no panel blur, and no default panel shadow. Use the shared
tokens rather than literal colors:

```css
:root {
  --bg-canvas: #111217;
  --bg-primary: #181b1f;
  --bg-secondary: #1e2228;
  --glass-bg: var(--bg-primary);
  --glass-bg-strong: var(--bg-secondary);
  --glass-border: rgba(255, 255, 255, 0.06);
  --glass-blur: 0px;
  --glass-radius: 8px;
  --text-primary: #e4e7ec;
  --text-muted: #9da5b4;
  --accent: #6e9fff;
  --success: #73bf69;
  --warning: #f2994a;
  --danger: #f2726f;
  --motion-fast: 120ms ease-out;
  --motion-base: 200ms ease-out;
}

[data-theme="light"] {
  --bg-canvas: #f8f8f8;
  --bg-primary: #ffffff;
  --glass-bg: #ffffff;
  --glass-border: #e5e5e5;
  --glass-blur: 0px;
  --text-primary: #1f1f1f;
  --text-muted: #525252;
  --accent: #005fb8;
  --success: #15732e;
  --warning: #8a5d00;
  --danger: #b5251f;
  --shadow-panel: none;
}
```

* Preserve the two established palettes; do not invent a third ad-hoc color
  system inside one component.
* Keep operational panels at 8px radius or less. Pills, status dots, and avatar
  circles are the exceptions.
* Dark panels may use restrained depth; light panels are flat with hairline
  borders. No neon, decorative orbs, or animated gradients.
* Motion: `prefers-reduced-motion` respected; transitions ≤ 200 ms ease-out.
* Iconography: `lucide-react`, stroke 1.5.
* Use `--font-sans` / `--font-mono`; both currently resolve to Inter so code-like
  values do not introduce an unrelated visual language.
* Components must be readable on a 1366×768 laptop and accessible (WCAG AA
  contrast against both dark and light surfaces).
